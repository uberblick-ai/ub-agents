"""Expendable local snapshot writer, isolated from launcher execution."""

import json
import os
from pathlib import Path
import select
import socket
import stat
import sys
import threading
import time

from .observations import (HEARTBEAT_SECONDS, MAX_BYTES, RETAINED_SESSIONS,
                           STALE_SECONDS, VERSION)
from .records import iso, seconds, timestamp


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError(f"Not a private owned directory: {path}")
    path.chmod(0o700)


def stale(state, now, host):
    if state.get("ended"):
        return True
    if state.get("host") == host and type(state.get("pid")) is int and state["pid"] > 0:
        try:
            os.kill(state["pid"], 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass
    return now - seconds(state["published_at"]) > STALE_SECONDS


def prune(directory, own, now, host):
    retained = []
    for path in directory.iterdir():
        if path.stem == own or path.suffix not in {".json", ".tmp"}:
            continue
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            old = now - info.st_mtime > STALE_SECONDS
            if path.suffix == ".json":
                with path.open("rb") as stream:
                    data = stream.read(MAX_BYTES + 1)
                try:
                    state = json.loads(data) if len(data) <= MAX_BYTES else {}
                    old = stale(state, now, host)
                except (ValueError, TypeError, KeyError, OverflowError):
                    pass
            if old:
                retained.append((info.st_mtime, path))
        except FileNotFoundError:
            continue  # Another publisher may have pruned it already.
    for _, path in sorted(retained, reverse=True)[RETAINED_SESSIONS:]:
        path.unlink(missing_ok=True)


def write_snapshot(directory, state):
    private_directory(directory.parent)
    private_directory(directory)
    state = state | {"published_at": iso(timestamp())}
    data = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ValueError("Snapshot exceeds byte limit")
    path = directory / f"{state['session']}.json"
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    prune(directory, state["session"], timestamp(), state["host"])


def run(root, receiver, life, errors, writer=write_snapshot):
    # A writer that never returns must not survive the launcher indefinitely.
    # The watchdog does no filesystem work and terminates only this helper.
    ended = threading.Event()

    def watch_parent():
        os.read(life, 1)  # Only EOF is sent; also arrives if the launcher is killed.
        ended.set()
        time.sleep(1)
        os._exit(0)

    threading.Thread(target=watch_parent, daemon=True).start()
    latest = None
    directory = Path(root) / ".ub-agents" / "sessions"
    try:
        while True:
            select.select([receiver, life], [], [], HEARTBEAT_SECONDS if latest else 0.1)
            # Capture EOF before draining. If EOF arrives during a write, take
            # another turn to receive the launcher's final queued snapshot.
            finishing = ended.is_set()
            # Bound draining so continuous activity cannot starve publication.
            for _ in range(256):
                try:
                    data = receiver.recv(MAX_BYTES + 1)
                except BlockingIOError:
                    break
                candidate = json.loads(data)
                if len(data) <= MAX_BYTES and candidate.get("version") == VERSION:
                    latest = candidate
            if latest:
                writer(directory, latest)
            if finishing:
                return
    except Exception as exc:
        os.write(errors, str(exc).encode("utf-8", errors="replace")[:1024])
    finally:
        os.close(errors)


def main():
    root, receiver_fd, life_fd, error_fd, sender_fd = sys.argv[1:]
    # Keep the inherited sender descriptor open for the lifetime of this helper.
    # Darwin discards queued datagrams on peer disconnect; this reference lets
    # the final snapshot survive launcher close/exit without any producer wait.
    receiver = socket.socket(fileno=int(receiver_fd))
    receiver.setblocking(False)
    run(root, receiver, int(life_fd), int(error_fd))


if __name__ == "__main__":
    main()
