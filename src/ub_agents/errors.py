class AgentError(Exception):
    """An actionable configuration, coordination, or execution failure."""


class RecordError(AgentError):
    """A trusted item's coordination record is malformed or contradictory."""


class LostOwnership(AgentError):
    """The launcher must terminate its execution before doing anything else."""


class CleanupError(AgentError):
    """Termination is inconclusive: preserve artifacts and stop the loop."""
