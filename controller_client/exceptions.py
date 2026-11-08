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


class UpdateError(ControllerError):
    """A self-update step failed.

    The message is plain text for the ``update_status`` ``failed`` detail; the
    old runner keeps running.
    """


class DeviceDisconnectedError(ExecutionError):
    """The phone or tablet is gone (unplugged, emulator stopped, session lost)
    or its Appium session is still starting; answered with
    ``ErrorCode.DEVICE_DISCONNECTED``."""


class TargetMismatchError(ExecutionError):
    """A desktop-only message on a phone target, or a phone message on a
    desktop target; answered with ``ErrorCode.TARGET_MISMATCH``."""


# --- Web browser layer (WEB_BROWSER_V1) -------------------------------------
# ``client.py`` maps these to the ErrorCode of the same name. Messages are
# short and fixed; the server shows its own wording for every one of them.


class WebBrowserSetupNeededError(ExecutionError):
    """The browser needs a one-time setup on the machine or device.

    ``step`` is a ``protocol.WebBrowserSetupStep`` value, sent as ``details``.
    """

    def __init__(self, step: str, message: str = "Browser setup needed") -> None:
        super().__init__(message)
        self.step = step


class WebBrowserUnsupportedError(ExecutionError):
    """The action does not exist for this browser, target or mode."""


class WebBrowserNotOpenError(ExecutionError):
    """No controllable browser is open (``named``: the requested browser
    value, or "" when none was named)."""

    def __init__(self, named: str) -> None:
        super().__init__("Browser not open")
        self.named = named


class WebBrowserAmbiguousError(ExecutionError):
    """Several browsers are open and none was named (``open_browsers``:
    WebBrowser values)."""

    def __init__(self, open_browsers: tuple[str, ...]) -> None:
        super().__init__("Several browsers are open")
        self.open_browsers = open_browsers


class WebScriptFailedError(ExecutionError):
    """A page script threw or timed out; ``error_class`` is the JavaScript
    error class name only (never the message or stack)."""

    def __init__(self, error_class: str) -> None:
        super().__init__("Script failed")
        self.error_class = error_class
