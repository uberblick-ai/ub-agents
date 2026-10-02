"""Fixed idle discovery budget, shared by ten launchers on one account."""

import math

# Reserve half of the common 5,000 REST requests/hour for busy loops and agents.
IDLE_REST_REQUESTS_PER_HOUR = 5000 * 0.5 / 10
IDLE_SECONDS_PER_REQUEST = 3600 / IDLE_REST_REQUESTS_PER_HOUR
IDLE_MAX_SECONDS = 3600
LOW_QUOTA_FRACTION = 0.2


def idle_interval(requests, minimum, quotas, now, elapsed):
    """Return a pass-start gap and low resources, using only observed headers.

    Reset bounds apply to the extra wait, never shortening the ordinary gap.
    With several low resources, the earliest reset bounds the doubling.
    """
    interval = min(IDLE_MAX_SECONDS, max(minimum, requests * IDLE_SECONDS_PER_REQUEST))
    low = {}
    for resource, headers in quotas.items():
        try:
            remaining = float(headers["x-ratelimit-remaining"])
            limit = float(headers["x-ratelimit-limit"])
            reset = float(headers["x-ratelimit-reset"])
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if (all(math.isfinite(value) for value in (remaining, limit, reset))
                and limit > 0 and remaining >= 0 and reset >= 0
                and remaining < limit * LOW_QUOTA_FRACTION):
            low[resource] = reset
    if low:
        interval = max(interval, min(interval * 2, IDLE_MAX_SECONDS,
                                     min(low.values()) - now + elapsed))
    return interval, frozenset(low)
