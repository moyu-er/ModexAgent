"""Exceptions for security module."""


class SecurityError(Exception):
    """Base exception for security-related errors."""
    pass


class CommandRejectedError(SecurityError):
    """Raised when a command is rejected by security policy."""
    pass


class ApprovalTimeoutError(SecurityError):
    """Raised when approval handler times out."""
    pass
