"""Bounded, private MCP stdio discovery; no tool calls or server output escape."""

import json
import os
import selectors
import subprocess
from time import monotonic

from .execution import stop_group


PROBE_TIMEOUT = 20
PROTOCOL_VERSION = "2024-11-05"
MAX_MESSAGE = 4 * 1024 * 1024


class ProbeFailure(Exception):
    """Only fixed, public reasons are carried to doctor."""


class Connection:
    def __init__(self, process, selector, deadline):
        self.process, self.selector, self.deadline = process, selector, deadline
        self.buffer = b""

    def send(self, value):
        data = json.dumps({"jsonrpc": "2.0", **value}).encode() + b"\n"
        # Nonblocking writes keep even an unresponsive server inside the deadline.
        with selectors.DefaultSelector() as writable:
            writable.register(self.process.stdin, selectors.EVENT_WRITE)
            while data:
                remaining = self.deadline - monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise ProbeFailure("timed out")
                try:
                    count = os.write(self.process.stdin.fileno(), data)
                except BlockingIOError:
                    continue
                data = data[count:]

    def receive(self, id):
        while True:
            if monotonic() >= self.deadline:
                raise ProbeFailure("timed out")
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, self.deadline - monotonic())):
                    raise ProbeFailure("timed out")
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ProbeFailure("exited")
                self.buffer += chunk
                if len(self.buffer) > MAX_MESSAGE:
                    raise ProbeFailure("handshake failed")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                raise ProbeFailure("handshake failed") from None
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                raise ProbeFailure("handshake failed")
            if "method" in value:
                # Servers can log notifications or request client capabilities.
                if "id" in value:
                    self.send({"id": value["id"], "error": {"code": -32601, "message": "Method not found"}})
                continue
            if value.get("id") != id or "error" in value or not isinstance(value.get("result"), dict):
                raise ProbeFailure("handshake failed")
            return value["result"]


def probe_stdio(command, *, cwd, env, timeout=PROBE_TIMEOUT, popen=None):
    """Return (tool count, failure reason), always terminating the owned group."""
    try:
        process = (popen or subprocess.Popen)(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                             start_new_session=True, bufsize=0)
    except FileNotFoundError:
        return None, "not found"
    except (OSError, ValueError):
        return None, "could not start"
    try:
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            connection = Connection(process, selector, monotonic() + timeout)
            connection.send({"id": 1, "method": "initialize", "params": {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "ub-agents-doctor", "version": "1"}}})
            result = connection.receive(1)
            if (result.get("protocolVersion") not in {PROTOCOL_VERSION, "2025-03-26", "2025-06-18", "2025-11-25"}
                    or not isinstance(result.get("capabilities"), dict)
                    or not isinstance(result.get("serverInfo"), dict)):
                raise ProbeFailure("handshake failed")
            connection.send({"method": "notifications/initialized"})
            count, cursor, seen = 0, None, set()
            while True:
                connection.send({"id": 2, "method": "tools/list", "params": {"cursor": cursor} if cursor else {}})
                result = connection.receive(2)
                tools = result.get("tools")
                if not isinstance(tools, list) or any(not isinstance(tool, dict) or
                                                     not isinstance(tool.get("name"), str) for tool in tools):
                    raise ProbeFailure("handshake failed")
                count += len(tools)
                cursor = result.get("nextCursor")
                if cursor is None:
                    return count, None
                if not isinstance(cursor, str) or not cursor or cursor in seen:
                    raise ProbeFailure("handshake failed")
                seen.add(cursor)
    except ProbeFailure as exc:
        return None, str(exc)
    except (OSError, ValueError, UnicodeError):
        return None, "exited" if process.poll() is not None else "handshake failed"
    finally:
        try:
            stop_group(process)
        finally:
            process.stdin.close()
            process.stdout.close()
