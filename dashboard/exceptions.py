from __future__ import annotations


class DashboardError(Exception):
    """Base for dashboard failures; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PresetLimitError(DashboardError):
    """The user already has the maximum number of saved filter presets."""

    def __init__(self, limit: int) -> None:
        super().__init__(
            f"You can save at most {limit} filter presets. Delete one first."
        )


class PresetNotFoundError(DashboardError):
    """The preset does not exist or belongs to another user."""

    def __init__(self) -> None:
        super().__init__("That saved filter no longer exists.")


class ExportThrottledError(DashboardError):
    """The user started too many CSV exports in the current window."""

    def __init__(self) -> None:
        super().__init__("Too many exports in a short time. Try again in a minute.")
