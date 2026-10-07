class AgentError(Exception):
    """An actionable configuration, coordination, or execution failure."""


class InstructionError(AgentError):
    """A configured instruction file is invalid or unreadable."""


class CheckoutRefreshError(AgentError):
    """An actionable control checkout refresh failure."""


class CheckoutSetupInterrupted(KeyboardInterrupt):
    """An interrupted install with its log and recovery step, still exit 130."""


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

    def __init__(self, message, *, next_step="confirm owned processes have exited"):
        super().__init__(message)
        self.next_step = next_step
