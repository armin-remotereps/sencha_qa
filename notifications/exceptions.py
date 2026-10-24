from __future__ import annotations


class NotificationError(Exception):
    """Base for notification failures; ``message`` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PushNotConfiguredError(NotificationError):
    """Web Push is disabled: the VAPID keys or subject are not set."""

    def __init__(self) -> None:
        super().__init__("Push notifications are not configured on this server.")


class PushEndpointNotAllowedError(NotificationError):
    """A subscription endpoint is not https or not on an allowed push service."""

    def __init__(self) -> None:
        super().__init__("This browser's push service is not supported.")
