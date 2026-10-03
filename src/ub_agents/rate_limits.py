"""Retry read-only GitHub operations without replaying any mutations."""

from .errors import GitHubError


READS = frozenset({
    "actor", "role", "timeline", "issue_content", "pr_content", "labels", "observe",
    "item", "active_milestone", "milestone_order", "blocked_by", "comments", "review_comments", "reviews",
    "repository_comments", "unminimized_comments", "candidate_evidence", "default_branch",
    "prs_for_branch", "dependency_graph",
})


class RateLimitReads:
    def __init__(self, github, wait):
        self.github = github
        self.wait = wait
        self.lease = None
        self.discovery = False

    def __getattr__(self, name):
        operation = getattr(self.github, name)
        if name not in READS:
            return operation

        def read(*args, **kwargs):
            while True:
                try:
                    return operation(*args, **kwargs)
                except GitHubError as exc:
                    if (not exc.rate_limited or (self.lease is None and not self.discovery)
                            or (self.lease is not None and self.lease["state"] not in {"claiming", "running"})):
                        raise
                    self.wait(exc, self.lease)
        return read

    @property
    def repository(self):
        return self.github.repository

    @repository.setter
    def repository(self, value):
        self.github.repository = value

    def claimed(self, lease):
        self.lease = lease
