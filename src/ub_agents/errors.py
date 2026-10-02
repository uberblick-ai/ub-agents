class AgentError(Exception):
    """An actionable configuration, coordination, or execution failure."""


class GitHubError(AgentError):
    """A named request failure, with conservative poll retry metadata."""

    def __init__(self, method, endpoint, detail, *, retryable=False, reset_at=None):
        super().__init__(f"GitHub {method} {endpoint} failed: {detail}")
        self.retryable = retryable
        self.reset_at = reset_at


class RecordError(AgentError):
    """A trusted item's coordination record is malformed or contradictory."""


class ValidationError(AgentError):
    """Observed GitHub state fails the assignment's acceptance contract."""


class LostOwnership(AgentError):
    """The launcher must terminate its execution before doing anything else."""


class CleanupError(AgentError):
    """Termination is inconclusive: preserve artifacts and stop the loop."""
