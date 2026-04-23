class AISAKError(Exception):
    """Base error for AISAK."""


class UserInputError(AISAKError):
    """Raised when user input is invalid."""


class PlaybackError(AISAKError):
    """Raised when playback cannot continue."""


class ConfigurationError(AISAKError):
    """Raised when the bot is misconfigured."""


class PermissionError(AISAKError):
    """Raised when Discord permissions are missing."""
