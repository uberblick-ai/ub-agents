"""A quiet, cancellable background sweep, with a flushed recovery log."""

import threading
import uuid
import subprocess

from .cleanup import Cleaner
from .cleanup_control import CleanupControl, CleanupLogError, CleanupStopped
from .diagnostic_retention import prune
from .errors import CleanupError
from .github import GitHub


def sweep(config, github, actor, *, session=None, control, failure=None):  # noqa: C901
    name = session or uuid.uuid4().hex
    path = config.root / ".ub-agents" / "sessions" / f"{name}.cleanup.log"
    try:
        if path.parent.resolve() != path.parent:
            raise OSError("Startup cleanup output directory redirects")
        path.parent.mkdir(mode=0o700, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            def emit(line):
                try:
                    stream.write(line + "\n")
                    stream.flush()
                except (OSError, ValueError) as exc:
                    raise CleanupLogError(str(exc)) from exc

            try:
                emit("Startup cleanup started")
                if failure is not None:
                    raise failure
                cleaner = Cleaner(config, github, actor, output=emit, control=control)
                cleaned = False
                try:
                    control.checkpoint()
                    cleaner.clean(apply=True)
                    cleaned = True
                except (CleanupStopped, CleanupLogError):
                    raise
                except BaseException as exc:
                    reason = str(exc) or type(exc).__name__
                    if isinstance(exc, CleanupError):
                        reason += f"; {exc.next_step}"
                    emit(f"Startup cleanup failed: {' '.join(reason.split())}; continuing to poll")
                control.checkpoint()
                prune(cleaner, emit, session=name, runs=cleaned)
                emit("Startup cleanup finished")
            except CleanupStopped:
                emit("Startup cleanup stopped")
            except CleanupLogError:
                raise
            except BaseException as exc:
                reason = str(exc) or type(exc).__name__
                emit(f"Startup cleanup failed: {' '.join(reason.split())}; continuing to poll")
    except BaseException:
        # Without a writable log nothing is removed. There is deliberately no
        # terminal fallback, including when creating the log itself fails.
        return


class StartupCleanup:
    def __init__(self, config, github, actor, stop, *, session=None):
        self.control = CleanupControl(stop)
        # Use the same transport/authentication with private request counters,
        # caches and rate-limit state. Recording fakes share their durable store.
        if isinstance(github, GitHub):
            runner = github.runner or subprocess.run

            def request(*args, **kwargs):
                self.control.checkpoint()
                return runner(*args, **kwargs)

            github = GitHub(github.repository, request)
        self.config, self.github, self.actor, self.session = config, github, actor, session
        self.thread = threading.Thread(target=sweep, name="startup-cleanup", daemon=True,
                                       args=(config, github, actor),
                                       kwargs={"session": session, "control": self.control})

    def start(self):
        try:
            self.thread.start()
        except Exception as exc:
            sweep(self.config, self.github, self.actor, session=self.session,
                  control=self.control, failure=exc)

    def close(self):
        self.control.close()
