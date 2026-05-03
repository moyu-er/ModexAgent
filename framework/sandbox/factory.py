import sys

from .adapters.base import SandboxAdapter
from .adapters.docker import DockerSandbox
from .adapters.e2b import E2BSandbox
from .adapters.landlock import LandlockSandbox
from .adapters.subprocess import SubprocessSandbox
from .config import SandboxConfig
from .enums import SandboxType
from .exceptions import SandboxUnavailableError

_ADAPTERS = {
    SandboxType.LANDLOCK: LandlockSandbox,
    SandboxType.SUBPROCESS: SubprocessSandbox,
    SandboxType.DOCKER: DockerSandbox,
    SandboxType.E2B: E2BSandbox,
}


def list_available_adapters() -> list[str]:
    available = []
    for sandbox_type, cls in _ADAPTERS.items():
        adapter = cls()
        if adapter.is_available:
            available.append(sandbox_type.value)
    return available


def get_default_sandbox(config: SandboxConfig | None = None) -> SandboxAdapter:
    return get_local_sandbox(config)


def get_local_sandbox(config: SandboxConfig | None = None) -> SandboxAdapter:
    if sys.platform == "linux":
        landlock = LandlockSandbox(config)
        if landlock.is_available:
            return landlock

        docker = DockerSandbox(config)
        if docker.is_available:
            return docker

    return SubprocessSandbox(config)


def get_cloud_sandbox(config: SandboxConfig | None = None) -> SandboxAdapter:
    e2b = E2BSandbox(config)
    if e2b.is_available:
        return e2b
    raise SandboxUnavailableError("E2B cloud sandbox is not available (check E2B_API_KEY)")


def get_sandbox(
    name: str | SandboxType,
    config: SandboxConfig | None = None,
) -> SandboxAdapter:
    if isinstance(name, SandboxType):
        name = name.value

    if name not in _ADAPTERS and name not in [t.value for t in SandboxType]:
        raise SandboxUnavailableError(f"Unknown sandbox adapter: {name}")

    for sandbox_type, cls in _ADAPTERS.items():
        if sandbox_type.value == name:
            adapter = cls(config)
            if not adapter.is_available:
                raise SandboxUnavailableError(f"Sandbox '{name}' is not available on this system")
            return adapter

    raise SandboxUnavailableError(f"Sandbox '{name}' is not available on this system")
