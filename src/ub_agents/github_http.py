"""Keep-alive GitHub HTTPS requests using gh's process-local authentication.

GitHub owns response policy and counters; this module only obtains the existing
credential and exchanges HTTP messages. Neither credentials nor raw transport
exceptions leave this boundary.
"""

from contextlib import contextmanager
import http.client
import json
import signal
import subprocess
import threading
from urllib.parse import urlsplit, urlunsplit

from .errors import GitHubError


REQUEST_TIMEOUT_SECONDS = 20


class Token:
    def __init__(self):
        self._lock = threading.Lock()
        self._loaded = False
        self._value = None
        self._secrets = set()

    def get(self, rejected=None):
        with self._lock:
            # Another reader may already have replaced the rejected credential.
            if not self._loaded or (rejected is not None and rejected == self._value):
                self._value = self._read()
                self._loaded = True
                if self._value:
                    self._secrets.add(self._value)
            return self._value

    def redact(self, text):
        with self._lock:
            for secret in sorted(self._secrets, key=len, reverse=True):
                text = text.replace(secret, "[redacted]")
        return text

    @staticmethod
    def _read():
        try:
            result = subprocess.run(
                ["gh", "auth", "token", "--hostname", "github.com"], capture_output=True,
                text=True, timeout=REQUEST_TIMEOUT_SECONDS, check=False)
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            return None
        if result.returncode in (-signal.SIGINT, 128 + signal.SIGINT):
            raise KeyboardInterrupt
        value = result.stdout.strip()
        if result.returncode or not value or any(c.isspace() for c in value):
            return None
        return value


# Shared by discovery, planning and startup cleanup, including failed lookups.
token = Token()


def request_target(endpoint):
    parts = urlsplit(endpoint)
    if (parts.scheme or parts.netloc) and (parts.scheme != "https" or parts.netloc != "api.github.com"):
        raise ValueError("GitHub HTTP requests require api.github.com")
    return urlunsplit(("", "", "/" + parts.path.lstrip("/"), parts.query, ""))


@contextmanager
def interruptible_request():
    # Launch records SIGINT without raising until owned execution can be
    # cleaned up. A foreground gh call used to deliver the interrupt here by
    # exiting; HTTP must provide the same unwind at the request boundary.
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGINT)
    if not callable(previous):
        yield
        return

    def interrupted(signum, frame):
        previous(signum, frame)
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupted)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


class Transport:
    def __init__(self):
        # http.client connections cannot be shared by concurrent readers.
        self._local = threading.local()

    def close(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            self._local.connection = None

    def send(self, endpoint, method, data, cached, credential):
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "ub-agents",
                   "Authorization": f"Bearer {credential}"}
        body = None
        if data is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(data).encode("utf-8")
        if cached is not None:
            headers["If-None-Match"] = cached[0]
        response, received = None, {}
        try:
            target = request_target(endpoint)
            if getattr(self._local, "connection", None) is None:
                self._local.connection = http.client.HTTPSConnection(
                    "api.github.com", timeout=REQUEST_TIMEOUT_SECONDS)
            connection = self._local.connection
            with interruptible_request():
                connection.request(method, target, body=body, headers=headers)
                response = connection.getresponse()
                for name, value in response.getheaders():
                    key = name.lower()
                    received[key] = f"{received[key]}, {value}" if key in received else value
                payload = response.read().decode("utf-8")
            if response.will_close:
                self.close()
            return response.status, {k: token.redact(v) for k, v in received.items()}, token.redact(payload), ""
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            reason = "timed out (20s)" if isinstance(exc, TimeoutError) else "connection failed"
            if response is not None:
                # Preserve an observed HTTP status and quota even when its body
                # is truncated. HTTP 4xx policy still takes precedence.
                return (response.status, {k: token.redact(v) for k, v in received.items()}, "",
                        "HTTPS operation timed out (20s)" if isinstance(exc, TimeoutError)
                        else "HTTPS connection reset while reading response")
            error = GitHubError(method, endpoint, f"HTTPS {reason}", retryable=True)
            error.probe_reason = reason
            # http.client exceptions can contain request headers. Do not retain
            # their text or chain them into launcher diagnostics.
            raise error from None
        except (ValueError, UnicodeError):
            self.close()
            raise GitHubError(method, endpoint, "Unreadable GitHub HTTPS request or response") from None
        except KeyboardInterrupt:
            self.close()
            raise
