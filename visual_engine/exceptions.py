class WorkerHandshakeError(Exception):
    """A vision worker's handshake was malformed, unauthenticated or incompatible.

    ``message`` is safe to send back to the worker; it never contains the token.
    """
