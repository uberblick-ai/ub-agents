class AgentError(Exception):
    """An actionable configuration, coordination, or execution failure."""


class LostOwnership(AgentError):
    """The launcher must terminate its execution before doing anything else."""


class CleanupError(AgentError):
    """Termination is inconclusive: preserve artifacts and stop the loop."""
