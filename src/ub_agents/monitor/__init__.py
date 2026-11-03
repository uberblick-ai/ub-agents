"""`ub-agents monitor`: every launcher's runs in a browser page on this machine."""

import errno
import subprocess
import sys
from time import monotonic, sleep
import webbrowser

from ..errors import AgentError
from ..records import timestamp
from .server import already_serving, create
from .snapshot import MonitorSource

START_SECONDS = 15


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


def monitor_background(config_path, root, port, open_page=True):
    """Start the monitor in its own session, so it outlives this terminal."""
    url = f"http://127.0.0.1:{port}/"
    if not already_serving(url):
        log = root / ".ub-agents" / "monitor.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "ub_agents", "monitor", "--config", str(config_path),
                 "--port", str(port), "--no-open"],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = monotonic() + START_SECONDS
        while not already_serving(url):
            if process.poll() is not None or monotonic() > deadline:
                raise AgentError(f"The monitor didn't start; see {log}")
            sleep(0.2)
        print(f"ub-agents monitor on {url} in the background (pid {process.pid}); stop it with: kill {process.pid}")
    else:
        print(f"The monitor already runs on {url}")
    if open_page:
        webbrowser.open(url)
    return 0
