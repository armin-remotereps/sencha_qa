class ControllerError(Exception):
    pass


class ConnectionError(ControllerError):
    pass


class AuthenticationError(ControllerError):
    pass


class HandshakeError(ConnectionError):
    """The handshake did not complete; reconnecting may succeed."""


class ConnectionTerminatedError(ControllerError):
    """The server closed the session with a close code that forbids reconnecting."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Connection terminated by the server (close code {code})")
        self.code = code


class ExecutionError(ControllerError):
    pass


class ProtocolError(ControllerError):
    pass


class UnknownMessageTypeError(ProtocolError):
    pass


class EnvironmentCheckError(ControllerError):
    pass


class ScreenCaptureError(ExecutionError):
    """The screen could not be captured, with the OS-level reason attached."""


class BrowserElementNotFoundError(ExecutionError):
    """The element index being acted on is not present on the page."""


class BrowserNotInstalledError(ExecutionError):
    """The requested browser is not installed on this machine."""


class InputBlockedError(ExecutionError):
    """Synthesized input would be discarded by the OS before reaching the screen."""


class DesktopSessionError(ExecutionError):
    """The desktop session refused or lost remote control.

    The message says why and how to fix it, for the agent and the person.
    """
