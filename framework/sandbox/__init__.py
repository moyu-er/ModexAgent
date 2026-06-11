# Sandbox module — isolated command/code execution with security guards.
# For usage examples, see examples/sandbox/.

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
from .env_builder import EnvBuilderConfig, EnvironmentBuilder, EnvPolicy
from .exceptions import (
    CommandRejectedError,
    SandboxError,
    SandboxTimeoutError,
    SandboxUnavailableError,
    WorkspaceBoundaryError,
)
from .factory import (
    get_cloud_sandbox,
    get_default_sandbox,
    get_local_sandbox,
    get_sandbox,
    list_available_adapters,
)
from .guard import (
    CommandPatternGuard,
    CommandPatternGuardConfig,
    CommandSeverity,
    GuardMatch,
    GuardResult,
)
from .guard_device import BENIGN_DEVICE_PATHS, is_benign_device_path
from .guard_pipeline import GuardPipeline
from .guard_traversal import PathTraversalConfig, PathTraversalGuard
from .platform import (
    Platform,
    get_default_shell,
    get_platform,
)
from .types import SandboxResult
from .workspace_policy import WorkspacePolicy, WorkspacePolicyConfig

__all__ = [
    "SandboxResult",
    "SandboxError",
    "SandboxUnavailableError",
    "SandboxTimeoutError",
    "CommandRejectedError",
    "WorkspaceBoundaryError",
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
    # Guard & policy
    "CommandPatternGuard",
    "CommandPatternGuardConfig",
    "CommandSeverity",
    "GuardMatch",
    "GuardResult",
    "GuardPipeline",
    "PathTraversalGuard",
    "PathTraversalConfig",
    "BENIGN_DEVICE_PATHS",
    "is_benign_device_path",
    "EnvironmentBuilder",
    "EnvBuilderConfig",
    "EnvPolicy",
    "WorkspacePolicy",
    "WorkspacePolicyConfig",
    # Platform
    "Platform",
    "get_platform",
    "get_default_shell",
    # Docker utils
    "check_docker_available",
    "check_windows_linux_containers",
    "DockerPlatformChecker",
]
