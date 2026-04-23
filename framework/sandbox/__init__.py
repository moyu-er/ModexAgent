from .types import SandboxResult
from .exceptions import SandboxError, SandboxUnavailableError, SandboxTimeoutError
from .config import SandboxConfig
from .enums import SandboxType
from .factory import (
    get_default_sandbox,
    get_sandbox,
    get_local_sandbox,
    get_cloud_sandbox,
    list_available_adapters,
)
from .platform import (
    Platform,
    get_platform,
    get_default_shell,
)
from .docker_utils import (
    check_docker_available,
    check_windows_linux_containers,
    DockerPlatformChecker,
)

from .adapters.base import SandboxAdapter
from .adapters.subprocess import SubprocessSandbox
from .adapters.landlock import LandlockSandbox
from .adapters.docker import DockerSandbox
from .adapters.e2b import E2BSandbox

# Import security from the new security package
from framework.security import (
    SecurityConfig,
    SecurityChecker,
    CommandPolicy,
    SecurityCheckResult,
    ApprovalHandler,
    ConsoleApprovalHandler,
    ConfigBasedApprovalHandler,
    APIBasedApprovalHandler,
    CompositeApprovalHandler,
    LoggingApprovalHandler,
)

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
