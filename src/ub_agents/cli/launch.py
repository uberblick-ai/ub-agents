"""Run the queue and own launcher startup, resources and error reporting."""

import os
from pathlib import Path
import signal
import sys

from ..config import LOCAL_CONFIG
from ..errors import (AgentError, CheckoutRefreshError, CleanupError, GitHubError, InstructionError)
from ..execution import repository_checks
from ..help import CommentHelpFormatter
from ..init.setup import runtime_guidance
from ..labels import configured_labels
from ..launch_log import launch_output
from ..launcher_lock import launcher_lock
from ..loop import POLL_RETRY_BASE_SECONDS, POLL_RETRY_MAX_SECONDS, _GracefulStop
from ..refresh import control_checkout_checks
from ..signals import SignalRelay
from ..startup_cleanup import StartupCleanup
from ._registry import command


def arguments(commands):
    launch = commands.add_parser("launch", help="run the queue in the foreground, or handle one item",
                                 formatter_class=CommentHelpFormatter,
                                 description="Run the queue in the foreground under the configured gates. Without a number, "
                                 "watch the queue; with a number, handle only that issue or PR, then exit. "
                                 "Use --agent NAME to serve only one configured agent; without a number, "
                                 "the queue keeps polling until you stop it.",
                                 examples=("ub-agents launch", "ub-agents launch --agent issue-preparer",
                                           "ub-agents launch 143 --agent implementer"))
    launch.add_argument("number", metavar="NUMBER", type=int, nargs="?", help="run only this item, then exit (optional)")
    launch.add_argument("--agent", metavar="NAME", help="serve only this configured agent (queue or NUMBER)")
    launch.add_argument("--once", action="store_true", help="observe once, run at most one assignment, then exit")
    launch.add_argument("--no-ui", action="store_true", help="plain lines instead of the terminal view")
    return launch


def launch_checks(config, github, read_git=None):
    for _, error in control_checkout_checks(config, github.default_branch(), read_git):
        if error is not None:
            raise error
    existing = {name.casefold() for name in github.labels()}
    missing = [label.name for label in configured_labels(config)
               if any(use.required for use in label.uses) and label.name.casefold() not in existing]
    if missing:
        raise AgentError(f"Missing GitHub workflow labels ({', '.join(missing)}); "
                         "run ub-agents doctor for setup commands")


def check_launch_setup(config, github, loop, *, interrupt, once=False):
    # No ownership exists yet: interrupt even a slow gh/git subprocess. The
    # normal in-run handlers defer interruption until execution can be cleaned up.
    def interrupt_checks(*_):
        interrupt()
        # subprocess.run kills and reaps on this exception. KeyboardInterrupt
        # instead assumes the terminal also signalled the child (UI keys do not).
        raise _GracefulStop

    handlers = {sig: signal.signal(sig, interrupt_checks)
                for sig in (signal.SIGINT, signal.SIGHUP)}
    delay = POLL_RETRY_BASE_SECONDS
    try:
        while True:
            loop.before_claim()
            loop.observe("activity", "checking setup")
            try:
                launch_checks(config, github)
            except GitHubError as exc:
                loop.before_claim()
                if once or not exc.retryable:
                    raise
                detail = " ".join(str(exc).split())
                loop.output(f"Skipped GitHub poll: {detail}; retrying in {delay:g}s")
                loop.wait(loop.stop_event, delay, "setup retry")
                delay = min(POLL_RETRY_MAX_SECONDS, delay * 2)
            else:
                loop.before_claim()
                loop.observe("activity", "polling")
                return
    except _GracefulStop:
        if loop.interrupt_event.is_set():
            raise KeyboardInterrupt from None
        raise
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)


def _validate_launch_agent(args, config):
    if args.agent is not None:
        if not any(agent.name == args.agent for agent in config.agents):
            names = ', '.join(agent.name for agent in config.agents)
            from . import parser
            parser().commands()["launch"].error(f"Unknown configured agent: {args.agent}; configured agents: {names}")


def _prepare_launch(config):
    for _, error in repository_checks(config):
        if error is not None:
            raise error
    local = config.root / ".ub-agents"
    local.mkdir(mode=0o700, exist_ok=True)


def _launch_signal_callbacks(loop, stop, interrupt):
    def wake_interrupt():
        interrupt.set()
        stop.set()

    def wake_stop():
        stop.set()
        loop.observe("activity", "stopping")

    relay = SignalRelay({"interrupt": wake_interrupt, "stop": wake_stop})

    def stop_now(*_):
        interrupt.request()
        stop.request()
        relay.request("interrupt")

    def stop_gracefully(*_):
        unwind = loop.request_graceful_stop()
        stop.request()
        relay.request("stop")
        if unwind:
            raise _GracefulStop

    return relay, stop_now, stop_gracefully


def _open_launch_view(args, config, loop, stop, session):
    if not stop.is_set():
        from ..launch_ui import open_view
        return open_view(config.root, session, args.launch_output, stop,
                         no_ui=args.no_ui, poll=loop.request_poll)
    return None


def _start_updates(config, loop):
    from ..updates import Updates
    loop.updates = Updates(config.root)
    if loop.updates is not None:
        loop.updates.start()


def _launch_queue(args, loop):
    if args.number is not None:
        loop.pass_messages.refusal_output = args.launch_output.refusals
        return loop.launch(once=True, number=args.number, agent_name=args.agent)
    if args.agent is not None:
        return loop.launch(once=args.once, agent_name=args.agent)
    loop.launch(once=args.once)


def _close_launch_resources(cleanup, loop, view):
    if cleanup is not None:
        cleanup.close()
    if loop.updates is not None:
        loop.updates.close()
    if view is not None:
        view.close()


def _finish_launch(handlers, relay, loop, publisher):
    for sig, handler in handlers.items():
        signal.signal(sig, handler)
    relay.close()
    loop.observe("close")
    if publisher is not None:
        publisher.close()


def launch_error_message(exc, root):
    # Request reads and continuous polling can wrap the original failure.
    # A failed fallback signal can also mask CleanupError with an OSError.
    errors = []
    error = exc
    while error is not None and error not in errors:
        errors.append(error)
        error = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    refresh = next((error for error in errors if isinstance(error, CheckoutRefreshError)), None)
    cleanup = next((error for error in errors if isinstance(error, CleanupError)), None)
    if refresh is not None:
        detail = str(refresh)
    elif cleanup is not None:
        detail = f"{cleanup}; {cleanup.next_step}, then launch again"
    elif any(isinstance(error, GitHubError) for error in errors):
        detail = str(exc)
        advice = "Fix the cause and restart ub-agents launch."
        no_retry = "ub-agents does not retry this error."
        if any(isinstance(error, GitHubError) and not error.retryable for error in errors) and no_retry not in detail:
            detail = f"{detail}; {no_retry} {advice}"
        if not detail.endswith(advice):
            detail = f"{detail}; {advice}"
    elif any(isinstance(error, InstructionError) for error in errors):
        detail = f"{root}: {exc}; restore readable, valid instruction files and launch again"
    else:
        return str(exc)
    return " ".join(detail.split())


def launch_resources(args, stack):
    stack.enter_context(launcher_lock(args.config.parent))
    args.launch_output = stack.enter_context(launch_output(args.config.parent))
    from ..launcher_code import startup_copy
    stack.enter_context(startup_copy())


def restart_launcher(restart, argv):
    # All launch resources have unwound; exec keeps the supervised PID and arguments.
    if restart.stop_event is not None and restart.stop_event.is_set():
        return 0
    try:
        os.execv(restart.executable, [restart.executable, *(sys.argv[1:] if argv is None else argv)])
    except OSError as exc:
        with launch_output(restart.root):
            print(f"ub-agents: automatic restart failed: {exc}", file=sys.stderr)
        return 1


@command("launch", arguments, number="optional", config_required=True,
         build=("config", "github", "coordinator", "loop"), identify_actor=False,
         validate=_validate_launch_agent, resources=launch_resources, error_message=launch_error_message)
def launch_run(args, config, github, actor, coordinator, loop, stop, interrupt):
    _prepare_launch(config)
    relay, stop_now, stop_gracefully = _launch_signal_callbacks(loop, stop, interrupt)
    from ..observations import Observations, Publisher
    publisher = None
    view = None
    cleanup = None
    handlers = {sig: signal.getsignal(sig)
                for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)}
    try:
        for sig in (signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop_now)
        signal.signal(signal.SIGTERM, stop_gracefully)
        try:
            publisher = Publisher(config.root, output=loop.output)
            loop.before_claim()
            loop.observer = Observations(config, actor, loop.config_path, publisher,
                                         clock=loop.coordinator.clock)
        except _GracefulStop:
            raise
        except Exception as exc:
            if publisher is not None:
                publisher.warning(str(exc))
            else:
                loop.output(f"Cannot publish launcher observations: {exc}")
        loop.before_claim()
        session = loop.observer.state['session'] if loop.observer is not None else None
        loop.observe("activity", "checking setup")
        view = _open_launch_view(args, config, loop, stop, session)
        if view is not None:
            loop.enable_poll_now()
        check_launch_setup(config, github, loop, once=args.once or args.number is not None,
                           interrupt=stop_now)
        _start_updates(config, loop)
        loop.before_claim()
        cleanup = StartupCleanup(config, github, loop.coordinator.actor, stop, session=session)
        loop.coordinator.cleanup_control = cleanup.control
        cleanup.start()
        loop.before_claim()
        return _launch_queue(args, loop)
    except _GracefulStop:
        return
    finally:
        try:
            _close_launch_resources(cleanup, loop, view)
        finally:
            _finish_launch(handlers, relay, loop, publisher)
