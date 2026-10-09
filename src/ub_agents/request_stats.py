"""Local request accounting; diagnostic output never performs GitHub reads."""

import threading
from time import monotonic


COUNTERS = ("gh_calls", "quota_requests", "not_modified_responses", "graphql_calls")


def request_counts(github):
    return {name: getattr(github, name, 0) for name in COUNTERS}


def request_delta(after, before):
    return {name: after[name] - before[name] for name in COUNTERS}


class PassOutput:
    """Coalesce consecutive equal passes across launcher and observation clients."""

    def __init__(self, output):
        self.output = output
        self.last = None
        self.lock = threading.Lock()

    def finished(self, kind, elapsed, counts, candidates):
        signature = (kind, counts, candidates)
        with self.lock:
            if signature != self.last:
                self.output(f"Discovery pass {kind}: {elapsed:.1f}s; "
                            f"gh calls={counts['gh_calls']}, REST quota={counts['quota_requests']}, "
                            f"HTTP 304={counts['not_modified_responses']}, "
                            f"GraphQL calls={counts['graphql_calls']}; candidates reached={candidates}")
            self.last = signature


class DiscoveryPass:
    def __init__(self, github, output, kind="empty"):
        self.github, self.output, self.kind = github, output, kind
        self.started = monotonic()
        self.before = request_counts(github)
        self.candidates = set()
        self.done = False

    def snapshot(self):
        return monotonic() - self.started, request_counts(self.github), len(self.candidates)

    def finish(self, kind=None, snapshot=None):
        if not self.done:
            elapsed, after, candidates = snapshot or self.snapshot()
            self.output.finished(kind or self.kind, elapsed, request_delta(after, self.before), candidates)
            self.done = True
