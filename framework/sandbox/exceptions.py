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
