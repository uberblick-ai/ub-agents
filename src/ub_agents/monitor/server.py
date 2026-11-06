"""Serve the browser monitor on this machine only.

The server reads GitHub with the launcher's gh login; the page gets the
snapshot, never a token. It answers only requests addressed to this machine
by name, so a site that points its own hostname here (DNS rebinding) gets
nothing, and refuses background requests that other sites make.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files
import json
import logging
import socket
import threading
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from ..errors import AgentError
from ..records import iso, timestamp

# A fixed port keeps the page's origin, so the browser remembers its
# notification permission between runs.
PORT = 4377
SIGNATURE = "X-Ub-Agents-Monitor"
# Several open tabs, or a reload, share one GitHub read.
REUSE_SECONDS = 10
HEADERS = {
    SIGNATURE: "1",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'"),
}


def allowed(host, port, path, site, mode):
    if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
        return False
    if site in {None, "same-origin", "none"}:
        return True
    # A link from another site may open the page, but never read its data.
    return mode == "navigate" and path == "/"


class Snapshots:
    """One GitHub read at a time, reused briefly unless the viewer asks for a fresh one."""

    def __init__(self, source, clock=monotonic):
        self.source = source
        self.clock = clock
        self.lock = threading.Lock()
        self.value = None
        self.read_at = None

    def get(self, fresh=False):
        with self.lock:
            if fresh or self.read_at is None or self.clock() - self.read_at >= REUSE_SECONDS:
                self.value = self.source.snapshot()
                self.read_at = self.clock()
            return self.value


class RepositorySnapshot(Snapshots):
    """Keep a repository's last successful read when its next read fails."""

    def __init__(self, source):
        super().__init__(source)
        self.value = {"repository": source.config.repository, "folder": str(source.config.root)}
        self.error = None

    def get(self, fresh=False):
        with self.lock:
            if fresh or self.read_at is None or self.clock() - self.read_at >= REUSE_SECONDS:
                try:
                    self.value = self.source.snapshot()
                except Exception as exc:
                    # One source cannot take the other repositories off the page.
                    if not isinstance(exc, (AgentError, OSError)):
                        logging.getLogger(__name__).exception(
                            "Couldn't refresh monitor repository %s", self.source.config.repository)
                    self.error = str(exc) or type(exc).__name__
                else:
                    self.error = None
                self.read_at = self.clock()
            return self.value | {"error": self.error}


class Repositories:
    """Read one source per checkout in parallel, with independent reuse windows."""

    def __init__(self, sources):
        self.snapshots = [RepositorySnapshot(source) for source in sources]
        self.lock = threading.Lock()

    def get(self, fresh=False):
        with self.lock:
            with ThreadPoolExecutor(max_workers=len(self.snapshots)) as workers:
                repositories = list(workers.map(lambda snapshot: snapshot.get(fresh), self.snapshots))
            return {"generated_at": iso(timestamp()), "local_host": socket.gethostname(),
                    "rates": self.rates(), "repositories": repositories}

    def rates(self):
        rates, newest = {}, {}
        counters = ("counted", "graphql_calls", "not_modified")
        for snapshot in self.snapshots:
            source = snapshot.source
            for key, value in source.rates().items():
                if key in counters:
                    rates[key] = rates.get(key, 0) + value
                else:
                    observed = source.github.resource_quota_times.get(key, float('-inf'))
                    if key not in newest or observed > newest[key]:
                        rates[key], newest[key] = value, observed
        return rates


def handler(snapshots, page):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = urlsplit(self.path)
            if not allowed(self.headers.get("Host"), self.server.server_address[1], parts.path,
                           self.headers.get("Sec-Fetch-Site"), self.headers.get("Sec-Fetch-Mode")):
                return self.reply(403, b"", "text/plain")
            if parts.path == "/":
                return self.reply(200, page, "text/html; charset=utf-8")
            if parts.path == "/snapshot.json":
                try:
                    data = snapshots.get(fresh="fresh" in parse_qs(parts.query))
                    return self.reply(200, json.dumps(data).encode(), "application/json")
                except AgentError as exc:
                    return self.reply(502, json.dumps({"error": str(exc)}).encode(), "application/json")
            return self.reply(404, b"", "text/plain")

        def reply(self, status, content, kind):
            try:
                self.send_response(status)
                for name, value in HEADERS.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # The viewer can leave while a snapshot is still being read.
                self.close_connection = True

        def log_message(self, format, *args):
            pass

    return Handler


def page():
    return files(__package__).joinpath("page.html").read_bytes()


def create(source, port=PORT):
    snapshots = source if isinstance(source, Repositories) else Snapshots(source)
    return ThreadingHTTPServer(("127.0.0.1", port), handler(snapshots, page()))


def already_serving(url):
    """Whether an earlier `ub-agents monitor` already answers on this port."""
    try:
        with urlopen(url, timeout=2) as response:
            return response.headers.get(SIGNATURE) == "1"
    except OSError:
        return False
