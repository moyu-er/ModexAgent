class BotServiceShutdownIncompleteError(RuntimeError):
    """Raised when workspace resources remain active after a stop attempt."""
