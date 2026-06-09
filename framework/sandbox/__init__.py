# EXPERIMENTAL: 此模块暂不推荐生产使用。当前零测试覆盖、零生产接入。
# 后续待补充测试和接入验证后再正式开放。如需使用，请参考 examples/sandbox/。

# Import security from the new security package
from framework.security import (
    APIBasedApprovalHandler,
    ApprovalHandler,
    CommandPolicy,
    CompositeApprovalHandler,
    ConfigBasedApprovalHandler,
    ConsoleApprovalHandler,
    LoggingApprovalHandler,
    SecurityChecker,
    SecurityCheckResult,
    SecurityConfig,
)

from .adapters.base import SandboxAdapter
from .adapters.docker import DockerSandbox
from .adapters.e2b import E2BSandbox
from .adapters.landlock import LandlockSandbox
from .adapters.subprocess import SubprocessSandbox
from .config import SandboxConfig
from .docker_utils import (
    DockerPlatformChecker,
    check_docker_available,
    check_windows_linux_containers,
)
from .enums import SandboxType
from .exceptions import SandboxError, SandboxTimeoutError, SandboxUnavailableError
from .factory import (
    get_cloud_sandbox,
    get_default_sandbox,
    get_local_sandbox,
    get_sandbox,
    list_available_adapters,
)
from .platform import (
    Platform,
    get_default_shell,
    get_platform,
)
from .types import SandboxResult

__all__ = [
    "SandboxResult",
    "SandboxError",
    "SandboxUnavailableError",
    "SandboxTimeoutError",
    "SandboxConfig",
    "SandboxType",
    "get_default_sandbox",
    "get_sandbox",
    "get_local_sandbox",
    "get_cloud_sandbox",
    "list_available_adapters",
    "SandboxAdapter",
    "SubprocessSandbox",
    "LandlockSandbox",
    "DockerSandbox",
    "E2BSandbox",
    # Security (re-exported from framework.security)
    "SecurityConfig",
    "SecurityChecker",
    "CommandPolicy",
    "SecurityCheckResult",
    # Approval Handlers
    "ApprovalHandler",
    "ConsoleApprovalHandler",
    "ConfigBasedApprovalHandler",
    "APIBasedApprovalHandler",
    "CompositeApprovalHandler",
    "LoggingApprovalHandler",
    # Platform
    "Platform",
    "get_platform",
    "get_default_shell",
    # Docker utils
    "check_docker_available",
    "check_windows_linux_containers",
    "DockerPlatformChecker",
]
