"""Install each new private worktree before starting its assigned agent."""

import shlex

from .checkout_setup import setup_environment
from .errors import AgentError, CheckoutSetupInterrupted, CleanupError, LostOwnership
from .execution import group_members, supervise
from .run_config import run_directory


def confirm_worktree_setup_stopped(config, run):
    """Retain a crashed run's ownership and artifacts until setup is known to end."""
    directory = run_directory(config.root, run) / "checkout-setup"
    try:
        # Compare against the resolved parent, so an aliased root such as macOS
        # /var -> /private/var is accepted while a redirected record is not.
        resolved = directory.parent.resolve() / directory.name
        if directory.resolve() != resolved:
            raise ValueError("redirected setup diagnostics")
        if not directory.exists():
            return
        stopped, pid = directory / "stopped", directory / "pid"
        if stopped.resolve() != resolved / "stopped" or pid.resolve() != resolved / "pid":
            raise ValueError("redirected setup process record")
        if stopped.exists():
            if stopped.read_text() != "confirmed\n":
                raise ValueError("unreadable setup stop record")
            return
        # Creation precedes Popen. No pid may mean a crash before its recording.
        if not pid.exists():
            raise ValueError("setup has no recorded process group or confirmed stop")
        group = int(pid.read_text())
        if group < 1:
            raise ValueError("invalid setup process group")
        if group_members(group):
            raise CleanupError(f"Worktree checkout setup process group {group} is still present",
                               next_step=f"confirm process group {group} has exited")
    except (OSError, ValueError) as exc:
        raise CleanupError(f"Worktree checkout setup process check cannot be confirmed: {exc}",
                           next_step=f"confirm setup has exited and repair its process records in {directory}") from exc


def run_worktree_setup(config, cwd, run_dir, interrupt, output, activity, *,
                       expires=None, process_started=None, pass_fds=(), observe_output=None):
    setting = config.checkout_setup
    if setting is None:
        return
    directory = run_dir / "checkout-setup"
    directory.mkdir(parents=True)
    log = directory / "process.log"
    log.touch()
    command = shlex.join(setting.command)
    output(f"checkout setup running in worktree {cwd}: {command} (log: {log})")
    activity("checkout setup running in worktree")
    confirmed = False
    try:
        try:
            code = supervise(list(setting.command), cwd, setup_environment(), directory,
                             setting.timeout_seconds, interrupt, expires=expires,
                             process_started=process_started, pass_fds=pass_fds,
                             observe_output=observe_output)
            confirmed = True
            if code:
                raise AgentError(f"{command} exited {code}")
        except CleanupError:
            raise
        except (AgentError, OSError, KeyboardInterrupt):
            # supervise either did not spawn or confirmed the whole group ended.
            confirmed = True
            raise
        finally:
            if confirmed:
                (directory / "stopped").write_text("confirmed\n")
    except LostOwnership:
        raise
    except (AgentError, OSError, KeyboardInterrupt) as exc:
        detail = f"{command} interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        message = f"checkout setup failed in worktree {cwd}: {detail} (log: {log})"
        if isinstance(exc, CleanupError):
            raise CleanupError(message, next_step=exc.next_step) from exc
        if isinstance(exc, KeyboardInterrupt):
            raise CheckoutSetupInterrupted(message) from exc
        raise AgentError(message) from exc
