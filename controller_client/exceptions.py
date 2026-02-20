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


class PrivilegeError(ControllerError):
    """Raised when the process lacks required OS privileges.

    Windows: requires Administrator elevation.
    Unix: requires root (UID 0).
    """
