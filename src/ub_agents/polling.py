"""Rolling discovery budget, shared by a launcher's claiming and observation passes."""

import math
import threading
from time import monotonic

# Reserve half of the common 5,000 REST requests/hour for busy loops and agents.
IDLE_REST_REQUESTS_PER_HOUR = 5000 * 0.5 / 10
IDLE_SECONDS_PER_REQUEST = 3600 / IDLE_REST_REQUESTS_PER_HOUR
DISCOVERY_REQUEST_CAPACITY = 125
IDLE_MAX_SECONDS = 3600
LOW_QUOTA_FRACTION = 0.2


class DiscoveryBudget:
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.balance = DISCOVERY_REQUEST_CAPACITY
        self.updated = clock()
        self.lock = threading.Lock()

    def _refill(self):
        now = self.clock()
        self.balance = min(DISCOVERY_REQUEST_CAPACITY,
                           self.balance + max(0, now - self.updated) / IDLE_SECONDS_PER_REQUEST)
        self.updated = now

    def debit(self, requests):
        with self.lock:
            self._refill()
            self.balance -= requests  # Keep debt, including spend from exempt passes.

    def wait_seconds(self):
        with self.lock:
            self._refill()
            return max(0, 1 - self.balance) * IDLE_SECONDS_PER_REQUEST


def poll_delay(seconds):
    if seconds < 60:
        return f"{round(seconds)}s"
    return f"{round(seconds / 60)} min"


def idle_interval(budget_wait, minimum, quotas, now, elapsed):
    """Return a pass-start gap and low resources, using only observed headers.

    Reset bounds apply to the extra wait, never shortening the ordinary gap.
    With several low resources, the earliest unexpired reset bounds the doubling.
    """
    interval = min(IDLE_MAX_SECONDS, max(minimum, elapsed + budget_wait))
    low = {}
    for resource, headers in quotas.items():
        try:
            remaining = float(headers["x-ratelimit-remaining"])
            limit = float(headers["x-ratelimit-limit"])
            reset = float(headers["x-ratelimit-reset"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if (all(math.isfinite(value) for value in (remaining, limit, reset))
                and limit > 0 and remaining >= 0 and reset > now
                and remaining < limit * LOW_QUOTA_FRACTION):
            low[resource] = reset
    if low:
        interval = max(interval, min(interval * 2, IDLE_MAX_SECONDS,
                                     min(low.values()) - now + elapsed))
    return interval, frozenset(low)
