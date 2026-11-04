"""Serve the browser monitor on this machine only.

The server reads GitHub with the launcher's gh login; the page gets the
snapshot, never a token. It answers only requests addressed to this machine
by name, so a site that points its own hostname here (DNS rebinding) gets
nothing, and refuses background requests that other sites make.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import threading
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen

from ..errors import AgentError

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


class Handler(BaseHTTPRequestHandler):
    snapshots: Snapshots
    page: bytes

    def handle(self):
        # A viewer that closes or reloads the tab during a GitHub read
        # leaves nobody to answer; that is not worth a traceback.
        try:
            super().handle()
        except ConnectionError:
            pass

    def do_GET(self):
        parts = urlsplit(self.path)
        if not allowed(self.headers.get("Host"), self.server.server_address[1], parts.path,
                       self.headers.get("Sec-Fetch-Site"), self.headers.get("Sec-Fetch-Mode")):
            return self.reply(403, b"", "text/plain")
        if parts.path == "/":
            return self.reply(200, self.page, "text/html; charset=utf-8")
        if parts.path == "/snapshot.json":
            try:
                data = self.snapshots.get(fresh="fresh" in parse_qs(parts.query))
                return self.reply(200, json.dumps(data).encode(), "application/json")
            except AgentError as exc:
                return self.reply(502, json.dumps({"error": str(exc)}).encode(), "application/json")
        return self.reply(404, b"", "text/plain")

    def reply(self, status, content, kind):
        self.send_response(status)
        for name, value in HEADERS.items():
            self.send_header(name, value)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        pass


def handler(snapshots, page):
    return type("MonitorHandler", (Handler,), {"snapshots": snapshots, "page": page})


def page():
    return files(__package__).joinpath("page.html").read_bytes()


def create(source, port=PORT):
    return ThreadingHTTPServer(("127.0.0.1", port), handler(Snapshots(source), page()))


def already_serving(url):
    """Whether an earlier `ub-agents monitor` already answers on this port."""
    try:
        with urlopen(url, timeout=2) as response:
            return response.headers.get(SIGNATURE) == "1"
    except OSError:
        return False
