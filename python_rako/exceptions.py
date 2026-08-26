class RakoBridgeError(Exception):
    pass


class RakoConnectionError(RakoBridgeError):
    """Raised when bridge connection fails."""

    pass


class RakoCommandError(RakoBridgeError):
    """Raised when command execution fails."""

    pass


class RakoDiscoveryError(RakoBridgeError):
    """Raised when bridge discovery fails or times out."""

    pass
