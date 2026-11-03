"""`ub-agents monitor`: every launcher's runs in a browser page on this machine."""

import errno
import webbrowser

from ..errors import AgentError
from ..records import timestamp
from .server import already_serving, create
from .snapshot import MonitorSource


def monitor_run(config, github, coordinator, port, open_page=True):
    url = f"http://127.0.0.1:{port}/"
    try:
        server = create(MonitorSource(config, github, coordinator, timestamp), port)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        if not already_serving(url):
            raise AgentError(f"Port {port} is taken by something else; pass --port to use another") from exc
        print(f"The monitor already runs on {url}")
        if open_page:
            webbrowser.open(url)
        return 0
    print(f"ub-agents monitor on {url} (Ctrl-C stops it)", flush=True)
    if open_page:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped the monitor")
    finally:
        server.server_close()
    return 0
