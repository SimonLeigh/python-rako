class RakoBridgeError(Exception):
    pass


class RakoConnectionError(RakoBridgeError):
    """Raised when bridge connection fails."""
    pass


class RakoCommandError(RakoBridgeError):
    """Raised when command execution fails."""
    pass
