from __future__ import annotations


class NotificationError(Exception):
    """Base for notification failures; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotificationsNotConfiguredError(NotificationError):
    """ntfy is disabled or misconfigured: NTFY_SERVER_URL or the prefix is invalid."""

    def __init__(self) -> None:
        super().__init__("Notifications are not configured on this server.")


class NotificationsOffError(NotificationError):
    """The user has not turned notifications on."""

    def __init__(self) -> None:
        super().__init__("Turn notifications on first.")


class TestNotificationThrottledError(NotificationError):
    """The user asked for test notifications too often."""

    def __init__(self, retry_seconds: int) -> None:
        super().__init__(
            f"A test notification was just sent. Try again in {retry_seconds} seconds."
        )
