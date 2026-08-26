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


class RakoUnsupportedCommandError(RakoBridgeError):
    """Raised when the selected transport cannot express a command.

    Distinct from :class:`RakoCommandError`, which means the bridge did not
    confirm a command it *could* have carried.  Retrying this one can never
    help, so command execution fails fast instead of resending.
    """


class RakoProtocolError(RakoBridgeError, ValueError):
    """Raised when a value cannot be encoded into a Rako frame.

    Subclasses :class:`ValueError` as well, so callers that already catch
    ``ValueError`` around encoding keep working.
    """
