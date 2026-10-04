"""In-memory, per-CLI pauses for a single launcher."""

import math

from .records import iso, timestamp

MARGIN_SECONDS = 60
FALLBACK_SECONDS = 15 * 60
MAX_RESET_SECONDS = 7 * 24 * 60 * 60


def number(value):
    try:
        result = float(value)
        return result if not isinstance(value, bool) and math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


class RuntimeUsage:
    def __init__(self, clock=timestamp, output=print):
        self.clock, self.output = clock, output
        self.pauses = {}

    def reset(self):
        self.pauses.clear()

    def paused(self, cli):
        end = self.pauses.get(cli)
        if end is None:
            return None
        if end <= self.clock():
            del self.pauses[cli]
            return None
        return {"reason": "usage limit reached", "ends_at": iso(end)}

    def bound_wait(self, delay):
        for cli in list(self.pauses):
            self.paused(cli)
        return min(delay, max(0, min(self.pauses.values()) - self.clock())) if self.pauses else delay

    def limit(self, cli, reset=None):
        now, reset = self.clock(), number(reset)
        usable = reset is not None and 0 < reset - now <= MAX_RESET_SECONDS
        end = reset + MARGIN_SECONDS if usable else now + FALLBACK_SECONDS
        previous = self.paused(cli)
        self.pauses[cli] = end
        if previous is None or previous["ends_at"] != iso(end):
            self.output(f"{cli} usage limit reached; pausing {cli} runs until {iso(end)}")
        return f"{cli} usage limit reached; resets {iso(reset if usable else end)}"
