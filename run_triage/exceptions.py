class RunTriageError(Exception):
    """A case analysis request that cannot be carried out; the message is user-facing."""


class TriageTurnClosedError(RunTriageError):
    """The analysis turn is no longer running (stopped, force-stopped or lost)."""
