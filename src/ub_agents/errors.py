class AgentError(Exception):
    """An actionable configuration, coordination, or execution failure."""


class GitHubError(AgentError):
    """A named request failure, with conservative poll retry metadata."""

    def __init__(self, method, endpoint, detail, *, retryable=False, reset_at=None, rate_limited=False):
        super().__init__(f"GitHub {method} {endpoint} failed: {detail}")
        self.retryable = retryable
        self.reset_at = reset_at
        self.rate_limited = rate_limited


class RecordError(AgentError):
    """A trusted item's coordination record is malformed or contradictory."""


class ValidationError(AgentError):
    """Observed GitHub state fails the assignment's acceptance contract."""


class TransitionPaused(ValidationError):
    """A human gate prevents transition; preserve the consecutive failure count."""


class RetryableExecutionError(AgentError):
    """A classified execution timeout or launch failure permits bounded retry."""


class RuntimePaused(AgentError):
    """Eligible runtimes are temporarily waiting for usage resets."""


class LostOwnership(AgentError):
    """The launcher must terminate its execution before doing anything else."""


class CleanupError(AgentError):
    """Termination is inconclusive: preserve artifacts and stop the loop."""
