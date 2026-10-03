"""Daily local runtime maintenance, serialized with launch reservations."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import tempfile
import time

from .execution import stop_group
from .runtime_installations import claude_updates_disabled, installation, updater

COOLDOWN_SECONDS = 24 * 60 * 60


class MaintenanceFailure(Exception):
    pass


@dataclass
class Reservation:
    executable: str
    descriptor: int
    guard: object

    def started(self):
        # Hold the start gate until Popen, not just until the claim. Otherwise a
        # native updater could race a reserved run that has not started yet.
        self.guard.close()


def state_directory():
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / ".local/state"
    return base / "ub-agents/runtime-updates"


@contextmanager
def lock(path, shared=False, create=True):
    """Kernel-owned locks have no stale PID markers or PID-reuse ambiguity."""
    if not create and not path.exists():
        yield None
        return
    stream = path.open("a+b" if create else "rb")
    try:
        try:
            fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            stream = None
        yield stream
    finally:
        if stream is not None:
            # Close instead of LOCK_UN: an inherited run descriptor must retain
            # the lock if the launcher dies before its child finishes.
            stream.close()


class RuntimeMaintenance:
    def __init__(self, output=print, stop_event=None, root=None, clock=None, which=None, runner=None):
        self.output = output
        self.stop_event = stop_event
        self.root = root if root is not None else state_directory()
        self.clock = clock or time.time
        self.which = which
        self.runner = runner or self._run
        self._unused = set()
        self._skipped = {}
        self._reserved = set()
        self._maintenance_fds = ()

    def paths(self, install):
        key = hashlib.sha256(install.identity.encode()).hexdigest()
        return tuple(self.root / (key + suffix) for suffix in (".guard", ".runs", ".json"))

    @staticmethod
    def read(path):
        try:
            state = json.loads(path.read_text())
            checked = state.get("checked")
            if not isinstance(checked, (int, float)) or isinstance(checked, bool) or not math.isfinite(checked):
                return {}
            return state
        except (OSError, ValueError, AttributeError):
            return {}

    def write(self, path, state):
        fd, temporary = tempfile.mkstemp(dir=self.root, prefix=path.name + ".")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(state, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def stopped(self):
        return self.stop_event is not None and self.stop_event.is_set()

    def _run(self, command, env, timeout, cancellable=True):
        if cancellable and self.stopped():
            raise MaintenanceFailure("shutdown requested")
        with tempfile.TemporaryFile() as output:
            try:
                process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                           pass_fds=self._maintenance_fds)
            except OSError as exc:
                raise MaintenanceFailure(f"cannot start command: {exc}") from exc
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if cancellable and self.stopped():
                        raise MaintenanceFailure("shutdown requested")
                    if time.monotonic() >= deadline:
                        raise MaintenanceFailure(f"timed out after {timeout:g}s")
                    time.sleep(0.05)
                if process.returncode:
                    raise MaintenanceFailure(f"command exited {process.returncode}")
                output.seek(0)
                return output.read(4096).decode("utf-8", errors="replace").strip()
            finally:
                stop_group(process)

    def version(self, cli, env, cancellable=True):
        install = installation(cli, self.which)
        if install is None:
            return None
        try:
            result = self.runner([install.executable, "--version"], env, 5, cancellable=cancellable)
            return " ".join(result.split()) or None
        except (MaintenanceFailure, OSError):
            return None

    def available(self, cli):
        """Read-only selection: opt-out projects also respect shared guards/health."""
        install = installation(cli, self.which)
        if install is None:
            return False
        guard, _, state = self.paths(install)
        if install.identity in self._reserved:
            return self.read(state).get("usable", True)
        try:
            if guard.exists():
                with lock(guard, shared=True, create=False) as held:
                    if held is None:
                        return False
                    return self.read(state).get("usable", True)
            return self.read(state).get("usable", True)
        except OSError:
            return False  # An unreadable guard cannot authorize a safe launch.

    @contextmanager
    def reserve(self, cli):
        """Atomically exclude maintenance before claiming; hold through cleanup."""
        install = installation(cli, self.which)
        if install is None:
            yield None
            return
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        guard, runs, state = self.paths(install)
        with lock(guard, shared=True) as held:
            if held is None or not self.read(state).get("usable", True):
                yield None
                return
            with lock(runs, shared=True) as active:
                if active is None:
                    yield None
                    return
                self._reserved.add(install.identity)
                try:
                    yield Reservation(install.executable, active.fileno(), held)
                finally:
                    self._reserved.discard(install.identity)

    def boundary(self, config):
        settings = config.runtime_updates
        used = {runtime.cli for agent in config.agents for runtime in agent.runtimes}
        policies = settings.policies if settings is not None else {}
        for cli in sorted(used | policies.keys()):
            if self.stopped():
                return
            if cli not in used:
                key = cli, str(policies[cli])
                if key not in self._unused:
                    self.output(f"Runtime maintenance {cli} (unused): skipped — no configured runtime uses {cli}")
                    self._unused.add(key)
                continue
            install = installation(cli, self.which)
            if install is None:
                key = cli, "missing"
                if settings is not None and key not in self._unused:
                    self.output(f"Runtime maintenance {cli} (missing): skipped — install the runtime manually; no updater run")
                    self._unused.add(key)
                continue
            try:
                if settings is None:
                    self.recover(cli, install)
                else:
                    self.check(cli, install, policies.get(cli, "off"), settings.timeout_seconds)
            except OSError as exc:
                self.output(f"Runtime maintenance {cli} ({install.method}): failed — warning: local state unavailable: {exc}")

    def recover(self, cli, install):
        """Recheck failed health without changing another project's update policy."""
        guard, _, state_path = self.paths(install)
        if self.read(state_path).get("usable", True):
            return
        with lock(guard) as held:
            if held is not None:
                self._recover_locked(cli, install, state_path, self.read(state_path), held)

    def _recover_locked(self, cli, install, state_path, state, held):
        if state.get("usable", True):
            return
        self._maintenance_fds = (held.fileno(),)
        try:
            version = self.version(cli, os.environ.copy())
        finally:
            self._maintenance_fds = ()
        if version is not None:
            # Preserve the completed check's cooldown and updater result.
            self.write(state_path, state | {"usable": True, "version": version})
            self.output(f"Runtime health {cli} ({install.method}) {version}: recovered — new runs can start")

    def check(self, cli, install, policy, timeout):
        reason = None
        if policy == "off":
            reason = "runtime-updates policy is off"
        elif cli == "claude":
            reason = claude_updates_disabled(os.environ)
        if reason is not None:
            # Project/launcher policy must not start the shared cooldown.
            self.recover(cli, install)
            key = cli, reason
            if key not in self._unused:
                self.output(f"Runtime maintenance {cli} ({install.method}): skipped — {reason}")
                self._unused.add(key)
            return
        guard, runs, state_path = self.paths(install)
        state = self.read(state_path)
        local_checked = self._skipped.get((cli, install.identity, policy), float("-inf"))
        if self.cooling_down(state) or self.clock() - local_checked < COOLDOWN_SECONDS:
            # Healthy cooldown reads need no exclusive guard, so they cannot
            # divert another launcher's start reservation for this tick.
            self.recover(cli, install)
            return
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with lock(guard) as held:
            if held is None:
                return  # Another check is in progress; no cooldown or repeat.
            state = self.read(state_path)
            if self.cooling_down(state):
                self._recover_locked(cli, install, state_path, state, held)
                return
            # Even command updates of native installations may replace files in place.
            in_place = not install.native or isinstance(policy, tuple)
            with lock(runs, shared=not in_place) as active:
                if active is None:
                    return  # Active run: deferral is not a completed check.
                # An orphan updater must keep excluding checks and starts after
                # a launcher crash, just as a still-running agent retains its lock.
                self._maintenance_fds = (held.fileno(), active.fileno())
                try:
                    self._check_locked(cli, install, policy, timeout, state_path, state, held)
                finally:
                    self._maintenance_fds = ()

    def cooling_down(self, state):
        # Older versions recorded automatic-policy skips in shared state.
        return (state.get("result") != "skipped" and
                self.clock() - state.get("checked", float("-inf")) < COOLDOWN_SECONDS)

    def _check_locked(self, cli, install, policy, timeout, state_path, state, held):
        before = None
        result, reason = "failed", None
        deadline = time.monotonic() + timeout
        env = os.environ.copy()

        def probe(command, probe_env):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MaintenanceFailure("maintenance timed out")
            return self.runner(command, probe_env, remaining)

        try:
            command, env, reason = updater(cli, install, policy, probe, env, self.which)
            if command is None:
                # Choosing auto cannot suppress another project's command.
                # Remember this skip only in this launcher; shared failed
                # health can still recover without resetting its cooldown.
                self._skipped[(cli, install.identity, policy)] = self.clock()
                self._recover_locked(cli, install, state_path, state, held)
                self.output(f"Runtime maintenance {cli} ({install.method}): skipped — {reason}")
                return
            remaining = deadline - time.monotonic()
            before = self.version(cli, os.environ.copy())
            deadline = time.monotonic() + remaining  # Version probes have their own bound.
            probe(command, env)
            result = "up-to-date"  # Classified by the next executable's version below.
        except (MaintenanceFailure, OSError, ValueError, TypeError) as exc:
            reason = str(exc)
        # Always re-resolve PATH after an attempted check, including failure/shutdown.
        after = self.version(cli, os.environ.copy(), cancellable=False)
        if after is None:
            result = "failed"
            reason = ((reason + "; ") if reason else "") + "runtime is unusable; no new runs will start"
        elif result == "up-to-date" and before != after:
            result = "updated"
        completed = {"checked": self.clock(), "usable": after is not None,
                     "version": after, "result": result, "identity": install.identity}
        self.write(state_path, completed)
        next_install = installation(cli, self.which)
        if next_install is not None and next_install.identity != install.identity:
            # Also retain cooldown/health when an operator updater changes
            # which executable PATH resolves for the next run.
            self.write(self.paths(next_install)[2], completed | {"identity": next_install.identity})
        versions = f" {before or '?'} -> {after or '?'}"
        detail = f" — {'warning: ' if result == 'failed' else ''}{reason}" if reason else ""
        self.output(" ".join(f"Runtime maintenance {cli} ({install.method}){versions}: {result}{detail}".split()))
