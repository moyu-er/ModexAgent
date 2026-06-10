class SandboxError(Exception):
    pass


class SandboxUnavailableError(SandboxError):
    pass


class SandboxTimeoutError(SandboxError):
    pass


class SandboxPermissionError(SandboxError):
    pass


class SandboxConfigurationError(SandboxError):
    pass


class CommandRejectedError(SandboxError):
    """Raised when a guard denies a command."""


class WorkspaceBoundaryError(SandboxError):
    """Raised when a path violates workspace policy."""
