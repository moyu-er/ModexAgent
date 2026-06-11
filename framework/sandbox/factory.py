from dataclasses import dataclass

from .adapters.base import SandboxAdapter
from .adapters.docker import DockerSandbox
from .adapters.e2b import E2BSandbox
from .adapters.landlock import LandlockSandbox
from .adapters.subprocess import SubprocessSandbox
from .config import SandboxConfig
from .enums import SandboxType
from .exceptions import SandboxUnavailableError
from .platform import Platform, get_platform


@dataclass(frozen=True)
class PlatformFallbackChain:
    """Ordered list of sandbox types to try, per platform."""

    linux: tuple[SandboxType, ...] = (
        SandboxType.LANDLOCK,
        SandboxType.DOCKER,
        SandboxType.SUBPROCESS,
    )
    macos: tuple[SandboxType, ...] = (
        SandboxType.DOCKER,
        SandboxType.SUBPROCESS,
    )
    windows: tuple[SandboxType, ...] = (
        SandboxType.DOCKER,
        SandboxType.SUBPROCESS,
    )
    unknown: tuple[SandboxType, ...] = (SandboxType.SUBPROCESS,)

    def for_platform(self, platform: Platform) -> tuple[SandboxType, ...]:
        match platform:
            case Platform.LINUX:
                return self.linux
            case Platform.MACOS:
                return self.macos
            case Platform.WINDOWS:
                return self.windows
            case _:
                return self.unknown


_DEFAULT_CHAIN = PlatformFallbackChain()

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


def get_local_sandbox(
    config: SandboxConfig | None = None,
    *,
    chain: PlatformFallbackChain | None = None,
) -> SandboxAdapter:
    """Get the best available local sandbox adapter for the current platform.

    Tries each adapter type in the platform's fallback chain until one is
    available.

    Args:
        config: Sandbox configuration.
        chain: Custom fallback chain. Uses platform defaults if not provided.
    """
    fallback_chain = chain or _DEFAULT_CHAIN
    platform = get_platform()

    for sandbox_type in fallback_chain.for_platform(platform):
        adapter_cls = _ADAPTERS.get(sandbox_type)
        if adapter_cls is not None:
            adapter = adapter_cls(config)
            if adapter.is_available:
                return adapter

    # Ultimate fallback — SubprocessSandbox is always available
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
