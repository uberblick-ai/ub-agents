"""One private artifact sweep before a launcher starts polling."""

from .cleanup import Cleaner
from .errors import AgentError, CleanupError


def sweep(config, github, actor, output=print, *, session=None):
    lines = []

    def emit(line):
        lines.append(line)
        output(line)

    try:
        Cleaner(config, github, actor, output=emit).clean(apply=True)
    except (AgentError, OSError) as exc:
        reason = str(exc)
        if isinstance(exc, CleanupError):
            reason += f"; {exc.next_step}"
        emit(f"Startup cleanup failed: {' '.join(reason.split())}; continuing to poll")

    if lines and session is not None:
        # Keep complete decisions outside the bounded observation envelope.
        # The view reads this immutable file on its local worker thread.
        path = config.root / ".ub-agents" / "sessions" / f"{session}.cleanup.log"
        try:
            if path.parent.resolve() != path.parent:
                raise OSError("Startup cleanup output directory redirects")
            path.parent.mkdir(mode=0o700, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
        except OSError as exc:
            output(f"Cannot publish startup cleanup output: {exc}")
    return bool(lines)
