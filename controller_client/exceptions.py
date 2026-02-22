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
