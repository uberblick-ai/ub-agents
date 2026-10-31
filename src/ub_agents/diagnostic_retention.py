"""Age-based startup retention of local diagnostics, without following links."""

from contextlib import contextmanager, nullcontext
import json
import os
import shutil
import socket
import stat

from .cleanup import Artifact
from .errors import AgentError
from .observations import MAX_BYTES
from .records import timestamp

RETENTION_SECONDS = 7 * 24 * 60 * 60
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@contextmanager
def removing(cleaner, artifact, output):
    control = cleaner.control
    with control.mutation(artifact) if control is not None else nullcontext():
        if control is not None:
            output(f"pruning {artifact.kind} {artifact.name}")
            control.checkpoint()
        yield


@contextmanager
def directory(root, name):
    # Anchor operations below the real project root. Opening each component
    # separately also refuses a parent replaced by a symlink during the sweep.
    local = os.open(root / ".ub-agents", DIRECTORY_FLAGS)
    try:
        child = os.open(name, DIRECTORY_FLAGS, dir_fd=local)
        try:
            yield child
        finally:
            os.close(child)
    finally:
        os.close(local)


def same_file(before, after):
    return (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_size) == (
        after.st_dev, after.st_ino, after.st_mtime_ns, after.st_size)


def run_files(parent, name):
    """Measure every regular file, rejecting unreadable or redirected contents."""
    descriptor = os.open(name, DIRECTORY_FLAGS, dir_fd=parent)
    try:
        newest, size = None, 0

        def failed(exc):
            raise exc

        for _, directories, files, folder in os.fwalk(".", dir_fd=descriptor,
                                                     follow_symlinks=False, onerror=failed):
            for entry in directories + files:
                info = os.stat(entry, dir_fd=folder, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise AgentError("Run diagnostics contain redirected or non-regular entries")
                file = os.open(entry, FILE_FLAGS, dir_fd=folder)
                try:
                    if not same_file(info, os.fstat(file)):
                        raise AgentError("Run diagnostics changed during inspection")
                finally:
                    os.close(file)
                newest = info.st_mtime if newest is None else max(newest, info.st_mtime)
                size += info.st_size
        # An empty directory has no newest file; use its own modification time.
        return newest if newest is not None else os.fstat(descriptor).st_mtime, size
    finally:
        os.close(descriptor)


def prune_runs(cleaner, parent, cutoff, removed, failed, output):  # noqa: C901, PLR0912
    for name in sorted(os.listdir(parent)):
        if cleaner.control is not None:
            cleaner.control.checkpoint()
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            link = stat.S_ISLNK(info.st_mode)
            if not link and not stat.S_ISDIR(info.st_mode):
                continue
            newest, size = (info.st_mtime, info.st_size) if link else run_files(parent, name)
            if newest >= cutoff or name in cleaner.kept_runs | cleaner.blocked_runs:
                continue
            artifact = Artifact("run", name, name)
            # A symlink is only unlinked, never read as diagnostics. Its leases
            # and surviving worktree/branch still protect the link itself.
            lease, _ = cleaner.run_state(artifact, check_diagnostics=not link)
            worktrees = cleaner.root / ".ub-agents" / "worktrees"
            if worktrees.resolve() != worktrees:
                continue  # An unregistered worktree cannot be ruled out here.
            inventory = cleaner.inventory()
            if any(a.run == name or (a.kind == "branch" and a.name == lease.get("branch"))
                   for a in inventory):
                continue
            # Fresh inspection after the coordination reads catches concurrent
            # log writes and refuses a replacement directory or symlink.
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not same_file(info, current):
                continue
            if not link:
                newest, size = run_files(parent, name)
                if newest >= cutoff:
                    continue
                with removing(cleaner, artifact, output):
                    shutil.rmtree(name, dir_fd=parent)
            else:
                with removing(cleaner, artifact, output):
                    os.unlink(name, dir_fd=parent)
            removed("runs", size)
        except FileNotFoundError:
            continue  # A concurrent sweep may already have removed this entry.
        except AgentError:
            continue  # Uncertain ownership or diagnostics always preserve logs.
        except OSError as exc:
            failed(f"run directory {name}", exc)


def inactive_snapshot(parent, name, now, host):
    # The isolated publisher is not needed by CLI commands that only import
    # startup cleanup (including the pinned in-run report entry point).
    from .observation_worker import stale

    try:
        descriptor = os.open(name, FILE_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        return True
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return False
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        state = json.loads(data) if len(data) <= MAX_BYTES else None
        return isinstance(state, dict) and stale(state, now, host)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    finally:
        os.close(descriptor)


def prune_sessions(cleaner, parent, own, now, removed, failed, output):  # noqa: C901, PLR0912, PLR0915
    host, cutoff = socket.gethostname(), now - RETENTION_SECONDS

    def remove_histories(session):
        name = session + ".histories"
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            with removing(cleaner, Artifact("session", name, name), output):
                if stat.S_ISLNK(info.st_mode):
                    os.unlink(name, dir_fd=parent)
                else:
                    shutil.rmtree(name, dir_fd=parent)
        except FileNotFoundError:
            pass

    def remove(name, *, companion=False):
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                return False
            if not companion and info.st_mtime >= cutoff:
                return False
            if not same_file(info, os.stat(name, dir_fd=parent, follow_symlinks=False)):
                return False
            with removing(cleaner, Artifact("session", name, name), output):
                os.unlink(name, dir_fd=parent)
            removed("sessions", info.st_size)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            failed(f"session file {name}", exc)
            return False

    names = sorted(os.listdir(parent))
    # Snapshots first, so companion logs can be removed with them even if newer.
    for name in sorted(names, key=lambda n: not n.endswith(".json")):
        if cleaner.control is not None:
            cleaner.control.checkpoint()
        suffix = next((s for s in (".json", ".tmp", ".cleanup.log") if name.endswith(s)), None)
        if suffix is None or name[:-len(suffix)] == own:
            continue
        session = name[:-len(suffix)]
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if info.st_mtime >= cutoff:
                continue
            if suffix == ".json":
                if not stat.S_ISLNK(info.st_mode) and not inactive_snapshot(parent, name, now, host):
                    continue
            elif not inactive_snapshot(parent, session + ".json", now, host):
                continue
            if not same_file(info, os.stat(name, dir_fd=parent, follow_symlinks=False)):
                continue
            if suffix == ".json":
                remove_histories(session)
            if remove(name) and suffix == ".json":
                if inactive_snapshot(parent, name, now, host):
                    remove(session + ".cleanup.log", companion=True)
        except FileNotFoundError:
            continue
        except OSError as exc:
            failed(f"session file {name}", exc)


def size_text(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}".removesuffix(".0") + f" {unit}"
        size /= 1024


def prune(cleaner, output, *, session=None, runs=True, now=None):
    now = timestamp() if now is None else now
    counts, freed, failures = {"runs": 0, "sessions": 0}, 0, set()

    def removed(kind, size):
        nonlocal freed
        counts[kind] += 1
        freed += size

    def failed(entry, exc):
        if entry not in failures:
            failures.add(entry)
            output(f"Cannot prune {entry}: {' '.join(str(exc).split())}; continuing to poll")

    for name in ("runs", "sessions") if runs else ("sessions",):
        if cleaner.control is not None:
            cleaner.control.checkpoint()
        try:
            with directory(cleaner.root, name) as parent:
                if name == "runs":
                    prune_runs(cleaner, parent, now - RETENTION_SECONDS, removed, failed, output)
                else:
                    prune_sessions(cleaner, parent, session, now, removed, failed, output)
        except FileNotFoundError:
            pass
        except OSError as exc:
            failed(f"{name} directory", exc)
    if any(counts.values()):
        output(f"pruned {counts['runs']} run directories and {counts['sessions']} session files "
               f"older than 7 days ({size_text(freed)})")
