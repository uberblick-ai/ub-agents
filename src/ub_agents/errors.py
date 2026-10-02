class AgentError(Exception):
    """An actionable configuration, coordination, or execution failure."""


class RecordError(AgentError):
    """A trusted item's coordination record is malformed or contradictory."""


class ValidationError(AgentError):
    """Observed GitHub state fails the assignment's acceptance contract."""


class TransitionPaused(ValidationError):
    """A human gate prevents transition; preserve the consecutive failure count."""


class RetryableExecutionError(AgentError):
    """A classified execution timeout or launch failure permits bounded retry."""


class LostOwnership(AgentError):
    """The launcher must terminate its execution before doing anything else."""


class CleanupError(AgentError):
    """Termination is inconclusive: preserve artifacts and stop the loop."""
