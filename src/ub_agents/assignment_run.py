"""Execute and settle a claimed assignment against the loop's live state."""

import json
import os
import socket
from dataclasses import replace

from .denials import collect_denials
from .errors import (AgentError, CheckoutSetupInterrupted, CleanupError, ExecutionStopped, GitHubError, LostOwnership,
                     RecordError, RetryableExecutionError, TransitionPaused, ValidationError)
from .execution import ScratchDirectory, Workspace, command_for, supervise
from .hooks import run_hook
from .launcher_code import descriptors as code_descriptors
from .report_command import launcher_report_command
from .records import iso
from .request_stats import request_counts, request_delta
from .run_branches import earlier_branches
from .run_config import record_process_group, run_config, run_directory
from .run_prompt import run_context, run_environment
from .run_usage import usage_fields
from .settlement import Settlement
from .usage_output import UsageOutput, collect_usage
from .worktree_setup import run_worktree_setup


class AssignmentRun:
    def __init__(self, loop):
        # Keep the loop itself so callbacks read its live objects and flags.
        self.loop = loop

    def execute(self, plan, lease, fresh, approval, instructions, requests_before, reservation=None, *,  # noqa: C901, PLR0912, PLR0915
                shared_instructions=""):
        loop = self.loop
        settlement = Settlement(loop.config, loop.github, loop.coordinator, loop.approvals)
        plan = replace(plan, item=replace(plan.item, head_repository=fresh.head_repository,
                                         base_branch=fresh.base_branch))
        run_dir = run_directory(loop.config.root.resolve(), lease["run"])
        scratch = ScratchDirectory(loop.config.root, loop.config.repository, lease["run"])
        preserve_scratch = False
        workspace = Workspace(loop.config, plan.agent, plan.item, lease, loop.github, output=loop.output)
        loop.output(f"#{plan.item.number} {plan.agent.name}: claimed {lease['run']} ({lease['runtime']})")
        loop.output(f"Logs: {run_dir}")

        def diagnostic(event, **details):
            loop._diagnostic(run_dir, event, **details)

        def record_uncertainty(exc):
            diagnostic("cleanup-unconfirmed", error=str(exc))
            cause = exc
            while cause is not None:
                if isinstance(cause, (LostOwnership, GitHubError)):
                    return  # GitHub failures and ownership loss forbid further writes.
                cause = cause.__context__
            try:
                loop.coordinator.assert_owned(lease)
                loop.coordinator.update(lease, cleanup="unconfirmed", summary=str(exc), action_needed=True)
            except (AgentError, KeyboardInterrupt) as failure:
                if isinstance(failure, KeyboardInterrupt):
                    loop.interrupt_event.set()
                    loop.stop_event.set()
                diagnostic("cleanup-verdict-unrecorded", error=str(failure))

        hook_attempted = False

        def cleanup_workspace(record=True):  # noqa: C901
            nonlocal preserve_scratch
            def before_remove():  # noqa: C901
                nonlocal hook_attempted
                if hook_attempted:
                    return False
                try:
                    reported = loop.coordinator.outcome(lease)
                except GitHubError:
                    if record:
                        raise
                    reported = outcome
                except (AgentError, KeyboardInterrupt) as exc:
                    if isinstance(exc, KeyboardInterrupt):
                        loop.interrupt_event.set()
                        loop.stop_event.set()
                    reported = outcome
                hook_attempted = True
                failure = run_hook(loop.config, lease, workspace.private, reported, branch=workspace.branch,
                                   expires=(lambda: loop.coordinator.deadline(lease)) if record else None,
                                   pass_fds=reservation.descriptors if reservation is not None else ())
                if failure:
                    if record:
                        try:
                            loop.coordinator.assert_owned(lease)
                            loop.coordinator.update(lease, cleanup_hook_error=failure)
                        except (LostOwnership, GitHubError):
                            raise
                        except (AgentError, KeyboardInterrupt) as exc:
                            if isinstance(exc, KeyboardInterrupt):
                                loop.interrupt_event.set()
                                loop.stop_event.set()
                            diagnostic("cleanup-hook-verdict-unrecorded", error=str(exc))
                    return False
                return True
            try:
                workspace.cleanup(before_remove)
            except CleanupError as exc:
                preserve_scratch = True
                if record:
                    record_uncertainty(exc)
                else:
                    diagnostic("cleanup-unconfirmed", error=str(exc))
                raise

        def cleanup_after_failure():
            try:
                cleanup_workspace()
            except (LostOwnership, GitHubError):
                loop._renewal.close()
                cleanup_workspace(record=False)
                raise

        def record_process(pid):
            loop.coordinator.assert_owned(lease)
            record_process_group(loop.config.root, lease["run"], pid)
            loop.coordinator.assert_owned(lease)

        def process_started(pid):
            if reservation is not None:
                reservation.started()
            record_process(pid)
            loop.observe("process", "running", "Supervision recorded a live process")

        def setup_process_started(pid):
            record_process(pid)
            loop.observe("process", "running", "Checkout setup running in worktree")

        interrupted = False
        stopped = False
        completing = False
        setup = True
        effect = "failure"
        result, summary = "retry", "Assignment ended without a validated outcome"
        outcome = None
        usage_output = None
        diagnostics = {}
        try:
            run_dir.mkdir(parents=True, exist_ok=True)
            loop.coordinator.assert_owned(lease)
            loop.coordinator.update(lease, state="running", started=True,
                                    attempt_effect="pending", host=socket.gethostname())
            loop.events.emit(loop.config, lease, "started")
            scratch.prepare()
            cwd = workspace.prepare()
            if workspace.created and not workspace.fork and loop.config.checkout_setup is not None:
                run_worktree_setup(loop.config, cwd, run_dir, loop.interrupt_event, loop.output,
                                   lambda state: loop.observe("activity", state),
                                   expires=lambda: loop.coordinator.execution_deadline(lease),
                                   process_started=setup_process_started,
                                   pass_fds=code_descriptors() + (reservation.descriptors if reservation is not None else ()),
                                   observe_output=lambda **_: loop._poll_updates())
                loop.observe("process", "exited", "Supervision confirmed worktree setup has ended")
                loop.observe("activity", "running assignment")
            current = loop.github.item(plan.item.number, plan.item.kind)
            loop.coordinator.observe_execution_item(lease, current)
            loop.coordinator.execution_deadline(lease)
            if current.state != "open" or not current.labels.intersection(plan.agent.triggers):
                raise AgentError("State or trigger changed before execution")
            if current.head != plan.item.head:
                raise AgentError("Candidate changed before execution")
            loop.coordinator.assert_owned(lease)
            report_command = launcher_report_command()
            context = run_context(
                loop.config.repository, plan, lease, approval.snapshot,
                feedback=loop.coordinator.feedback(plan.item, plan.agent.name),
                scratch=scratch.path, report_command=report_command, branch=workspace.branch,
                earlier_branches=earlier_branches(loop.coordinator, plan.item, plan.agent, lease["run"]))
            context_path = run_dir / "context.json"
            context_path.write_text(json.dumps(context, indent=2))
            run_config_path = run_dir / "run.json"
            run_config_path.write_text(json.dumps(run_config(loop.config, plan.agent, lease)))
            env = run_environment(os.environ.copy(), context, lease["id"], context_path, run_config_path)
            diagnostic("started", cwd=str(cwd))
            setup = False
            command = command_for(plan.agent, plan.runtime, scratch.path, report_command)
            if reservation is not None and reservation.executable is not None:
                command[0] = reservation.executable
            if plan.runtime:
                usage_output = UsageOutput(plan.runtime.cli, run_dir, loop.usage, env)
            def observe_output(final=False):
                loop._poll_updates()
                if usage_output:
                    usage_output.poll(final=final)
            try:
                code = supervise(command, cwd, env, run_dir,
                                 plan.agent.timeout_seconds, loop.interrupt_event,
                                 loop.prompt_for(plan, lease, context, instructions,
                                                 shared_instructions=shared_instructions) if plan.runtime else None,
                                 expires=lambda: loop.coordinator.execution_deadline(lease), process_started=process_started,
                                 process_exited=lambda: loop.coordinator.execution_finished(lease),
                                 observe_output=observe_output if usage_output or loop.updates else None,
                                 pass_fds=code_descriptors() + (reservation.descriptors if reservation is not None else ()))
            finally:
                loop.coordinator.execution_finished(lease)
                cli = plan.runtime.cli if plan.runtime else None
                diagnostics = (collect_denials(cli, run_dir / "process.log")
                               | collect_usage(cli, run_dir / "process.log"))
            loop.coordinator.execution_deadline(lease)
            if usage_output:
                usage_output.poll(final=True)
            loop.observe("process", "exited", "Supervision confirmed execution has ended")
            diagnostic("execution-exited", code=code)
            loop.github.begin_finalization()
            # No acceptance or release until all attributable execution has ended.
            cleanup_workspace()
            completing = True
            loop._finalizing = True
            loop.coordinator.assert_owned(lease)
            outcome = loop.coordinator.outcome(lease)
            if outcome is not None and diagnostics:
                loop.coordinator.update_outcome(lease, outcome, **diagnostics)
            if outcome is None:
                result = "retry"
                summary = f"Execution exited {code} without an explicit GitHub outcome; inspect process.log"
            else:
                result, summary, effect = settlement.reported_verdict(lease, plan, outcome, lease, "completion")
        except CleanupError as exc:
            loop.observe("process", "unknown", str(exc))
            preserve_scratch = True
            record_uncertainty(exc)
            raise
        except LostOwnership as exc:
            diagnostic("ownership-lost", error=str(exc))
            loop._renewal.close()
            cleanup_workspace(record=False)
            # Do not report, release, or accept after losing ownership.
            raise
        except GitHubError as exc:
            diagnostic("github-failed", error=str(exc))
            loop._renewal.close()
            cleanup_workspace(record=False)
            # The lease and any stored report remain for expiry recovery.
            raise
        except KeyboardInterrupt as exc:
            if completing:
                diagnostic("transition-interrupted", outcome=outcome["id"] if outcome else None)
                cleanup_after_failure()
                # A request can be interrupted after GitHub applied its write,
                # before our in-memory outcome reflects it. Even a read before
                # transition start must preserve the report for expiry recovery.
                raise
            interrupted = True
            effect = "unchanged"
            summary = "Launcher interrupted; attributable execution terminated"
            if isinstance(exc, CheckoutSetupInterrupted):
                summary += f"; {exc}"
            cleanup_after_failure()
        except ExecutionStopped as exc:
            stopped = True
            result, summary, effect = "blocked", str(exc), "unchanged"
            loop.coordinator.execution_finished(lease)
            # Persist the supervision verdict before cleanup so an early report
            # cannot become recoverable if cleanup or release is interrupted.
            # Closure needs no action notice, including after a launcher restart.
            try:
                loop.coordinator.assert_owned(lease)
                loop.coordinator.update(lease, result=result, summary=summary,
                                        attempt_effect=effect, unreported=True,
                                        **({"action_needed": False} if exc.item_closed else {}))
            except (LostOwnership, GitHubError):
                loop._renewal.close()
                cleanup_workspace(record=False)
                raise
            cleanup_after_failure()
        except TransitionPaused as exc:
            result, summary, effect = "blocked", str(exc), "unchanged"
            cleanup_after_failure()
        except Exception as exc:
            if completing and not isinstance(exc, ValidationError):
                raise  # Keep completed reports recoverable; never invent a failure verdict.
            result = ("retry" if isinstance(exc, RetryableExecutionError)
                      or (setup and isinstance(exc, (AgentError, OSError))
                          and not isinstance(exc, (RecordError, ValidationError))) else "blocked")
            summary = str(exc)
            cleanup_after_failure()
        finally:
            if not preserve_scratch:
                try:
                    scratch.cleanup()
                except AgentError as exc:
                    # Scratch removal cannot invalidate confirmed process termination
                    # or prevent release of a completed run's lease.
                    diagnostic("scratch-removal-failed", path=str(scratch.path), error=str(exc))
                    loop.output(f"Scratch removal failed: {exc}")
        if usage_output and usage_output.reached and effect != "reset" and not interrupted and not stopped:
            result, effect = "retry", "unchanged"
            summary = usage_output.summary
            completing = False  # This supervision verdict supersedes an early report.
        loop.github.begin_finalization()
        if outcome is None:
            # A report can precede a timeout/interruption. Keep that report
            # unaccepted and persist the supervisor's actual verdict on the lease.
            outcome = loop.coordinator.outcome(lease)
        if stopped:
            if outcome is not None and usage_fields(diagnostics):
                loop.coordinator.update_outcome(lease, outcome, **usage_fields(diagnostics))
            loop.coordinator.release(lease, result, summary, attempt_effect=effect, unreported=True,
                                     **usage_fields(diagnostics))
        else:
            settlement.settle(plan, lease, lease["attempt"], result, summary, effect, outcome, diagnostics,
                              completed=completing)
        loop.github.end_finalization()
        loop._renewal.close()
        diagnostic("released", result=result, summary=summary,
                   github_requests=request_delta(request_counts(loop.github), requests_before))
        loop._replan_finished_item(plan)
        loop.output(f"#{plan.item.number} {plan.agent.name}: {result} — {summary}")
        if result == "blocked":
            loop.pass_messages.released_blocker(plan, summary)
        if interrupted or loop.interrupt_event.is_set():
            raise KeyboardInterrupt
        return True
