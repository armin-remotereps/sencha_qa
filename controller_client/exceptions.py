class ControllerError(Exception):
    pass


class ConnectionError(ControllerError):
    pass


class AuthenticationError(ControllerError):
    pass


class ExecutionError(ControllerError):
    pass


class ProtocolError(ControllerError):
    pass


class UnknownMessageTypeError(ProtocolError):
    pass


class EnvironmentCheckError(ControllerError):
    pass


class InputBlockedError(ExecutionError):
    """Synthesized input would be discarded by the OS before reaching the screen."""


class OmniParserError(ExecutionError):
    """An OmniParser load or find_element failure tagged with where it happened.

    ``code`` is the string value of a ``protocol.ErrorCode`` member; it is kept
    as a plain string because ``protocol`` imports this module.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        device: str,
        weights_dir: str,
        code: str,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.device = device
        self.weights_dir = weights_dir
        self.code = code

    def details(self) -> str:
        return (
            f"phase={self.phase}; device={self.device}; "
            f"weights_dir={self.weights_dir}"
        )
