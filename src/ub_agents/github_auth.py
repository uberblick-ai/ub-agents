"""Share continuous-launch HTTP 401 timing across GitHub clients and threads."""

from datetime import datetime
import threading


class AuthenticationRetry:
    def __init__(self):
        self.enabled = False
        self._lock = threading.Lock()
        self._since = None
        self._delay = 60

    @property
    def failing(self):
        with self._lock:
            return self._since is not None

    def rejected(self):
        with self._lock:
            if self._since is None:
                self._since = datetime.now().strftime("%H:%M")

    def succeeded(self):
        with self._lock:
            self._since = None
            self._delay = 60

    def poll_failure(self):
        with self._lock:
            # Another thread may have succeeded since this poll failed.
            since = self._since or datetime.now().strftime("%H:%M")
            delay = self._delay
            self._delay = min(900, delay * 2)
            return delay, (f"GitHub rejects the token (HTTP 401) since {since}; "
                           f"retrying in {delay // 60}m. Check gh auth status.")
