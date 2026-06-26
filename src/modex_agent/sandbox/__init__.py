# Sandbox module — isolated command/code execution with security guards.
#
# Public seam (ADR-0005 facade-only; slimmed in candidate ⑤ Part A per
# ADR-0007):
#   - selection entry points (get_default_sandbox / get_sandbox /
#     get_local_sandbox / get_cloud_sandbox / list_available_adapters)
#   - the SandboxAdapter ABC (the extension contract)
#   - consumer-facing types/errors
#
# Concrete adapters live behind `sandbox.adapters`; command/path guards
# behind `sandbox.guard` / `sandbox.guard_*`; env builder behind
# `sandbox.env_builder`; workspace policy behind `sandbox.workspace_policy`;
# platform/docker helpers behind `sandbox.platform` / `sandbox.docker_utils`.
# For usage examples, see examples/sandbox/.

from .adapters.base import SandboxAdapter
from .config import SandboxConfig
from .enums import SandboxType
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
from .types import SandboxResult

__all__ = [
    # Selection entry points
    "get_default_sandbox",
    "get_sandbox",
    "get_local_sandbox",
    "get_cloud_sandbox",
    "list_available_adapters",
    # Seam ABC
    "SandboxAdapter",
    # Types / errors
    "SandboxConfig",
    "SandboxResult",
    "SandboxType",
    "SandboxError",
    "SandboxUnavailableError",
    "SandboxTimeoutError",
    "CommandRejectedError",
    "WorkspaceBoundaryError",
]
