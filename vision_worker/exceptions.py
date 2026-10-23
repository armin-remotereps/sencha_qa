class VisionWorkerError(Exception):
    pass


class ProtocolError(VisionWorkerError):
    pass


class UnknownMessageTypeError(ProtocolError):
    pass


class HandshakeError(VisionWorkerError):
    """The handshake did not complete; reconnecting may succeed."""


class AuthenticationError(VisionWorkerError):
    """The server rejected the token or the worker version; reconnecting will not help."""


class ConnectionTerminatedError(VisionWorkerError):
    """The server closed the session with a close code that forbids reconnecting."""

    def __init__(self, code: int) -> None:
        super().__init__(f"Connection terminated by the server (close code {code})")
        self.code = code


class EngineError(VisionWorkerError):
    """An OmniParser load or parse failure tagged with where it happened."""

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        device: str,
        weights_dir: str,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.device = device
        self.weights_dir = weights_dir

    def details(self) -> str:
        return (
            f"phase={self.phase}; device={self.device}; "
            f"weights_dir={self.weights_dir}"
        )
