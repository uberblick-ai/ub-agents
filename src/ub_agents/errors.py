class AgentError(Exception):
    """An actionable configuration, coordination, or execution failure."""

    def __init__(self, message, *, detail=None, next_step=None):
        super().__init__(message)
        self.detail = detail or message
        self.next_step = next_step


class InstructionError(AgentError):
    """A configured instruction file is invalid or unreadable."""


class ConfigurationError(AgentError):
    """Invalid configuration, with its selected file and optional setup action."""

    def __init__(self, message, *, path=None, next_step=None, detail=None):
        super().__init__(message, detail=detail)
        self.path = path
        self.next_step = next_step


class UnsupportedConfiguration(ConfigurationError):
    """A configuration key or value may require a newer launcher."""


class CheckoutRefreshError(AgentError):
    """An actionable control checkout refresh failure."""


class CheckoutSetupInterrupted(KeyboardInterrupt):
    """An interrupted install with its log and recovery step, still exit 130."""


class GitHubError(AgentError):
    """A named request failure, with conservative poll retry metadata."""

    def __init__(self, method, endpoint, detail, *, retryable=False, reset_at=None,
                 rate_limited=False, status=None, not_sent=False, response_message=None):
        super().__init__(f"GitHub {method} {endpoint} failed: {detail}")
        self.method, self.endpoint, self.detail = method, endpoint, detail
        self.response_message = response_message
        self.response_output = ""
        self.retryable = retryable
        self.reset_at = reset_at
        self.rate_limited = rate_limited
        self.status = status
        self.not_sent = not_sent


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

    def __init__(self, message, clis=()):
        super().__init__(message)
        self.clis = clis


class LostOwnership(AgentError):
    """The launcher must terminate its execution before doing anything else."""


class ExecutionStopped(AgentError):
    """A stop label or closed item cancels execution without charging an attempt."""

    def __init__(self, message, *, item_closed=False):
        super().__init__(message)
        self.item_closed = item_closed


class CleanupError(AgentError):
    """Termination is inconclusive: preserve artifacts and stop the loop."""

    def __init__(self, message, *, next_step="confirm owned processes have exited"):
        super().__init__(message)
        self.next_step = next_step
