"""Typed concrete runtime selection — the single probe + factory owner.

Replaces the lossy ``(effective_backend, degraded_reason)`` tuple with one
frozen, typed :class:`SandboxSelection` value, and owns BOTH halves of
runtime choice so no other layer duplicates them:

- :func:`resolve_selection` - owns engine availability selection.
- :func:`select_runtime` — the ONLY factory for concrete runtimes
  (BwrapRuntime / SeatbeltRuntime / OciContainerRuntime / HostRuntime).

Concrete runtimes compile an already-selected substrate and validate startup
without repeating engine selection. macOS LOCAL/AUTO selects SeatbeltRuntime;
Podman-only hosts construct ``OciContainerRuntime(engine=
OciEngine.PODMAN)`` — typed, never a defaulted docker string; Windows AUTO
is HostRuntime with an actionable degraded_reason naming the declared
alternatives (oci / host). AUTO and LOCAL never fall to the
oci family, OCI never falls to LOCAL (model validation makes the illegal
combinations unrepresentable); ``DEFAULT`` never reaches selection (program
error); ``AUTO`` is resolved away here and never survives as effective.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, model_validator

from .engine_probe import probe_bwrap, probe_docker, probe_podman, probe_seatbelt
from .platform import Platform, get_platform
from .settings import LocalSandboxEngine, OciEngine, SandboxBackend

if TYPE_CHECKING:
    from .runtime import SandboxRuntime

_get_platform = get_platform

__all__ = [
    "LocalSandboxEngine",
    "OciEngine",
    "SandboxSelection",
    "resolve_selection",
    "select_runtime",
]


class SandboxSelection(BaseModel):
    """The effective execution substrate after tier resolution + probing.

    Carries everything the runtime factory needs: the requested tier, the
    effective backend, the platform the choice was made on, the concrete
    engine enum for the selected family, and the degradation fact. Model
    validation makes illegal combinations unrepresentable — the factory can
    trust the value without re-checking.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    requested: SandboxBackend
    effective: SandboxBackend
    platform: Platform
    local_engine: LocalSandboxEngine | None = None
    oci_engine: OciEngine | None = None
    degraded_reason: str | None = None

    @model_validator(mode="after")
    def _validate_combinations(self) -> SandboxSelection:
        if self.requested is SandboxBackend.DEFAULT:
            raise ValueError(
                "DEFAULT is not a runtime tier — the assembly layer must "
                "keep the sandbox dormant when backend == DEFAULT"
            )
        if self.effective is SandboxBackend.DEFAULT:
            raise ValueError(
                "effective backend DEFAULT is unconfigured, never a selection"
            )
        if self.effective is SandboxBackend.AUTO:
            raise ValueError(
                "AUTO is not an effective backend — resolve_selection must "
                "resolve it to a concrete tier"
            )
        if self.effective is SandboxBackend.LOCAL:
            self._validate_local()
        elif self.effective is SandboxBackend.OCI:
            self._validate_oci()
        else:
            self._validate_host()
        self._validate_reason_discipline()
        return self

    def _validate_local(self) -> None:
        if self.requested is SandboxBackend.OCI:
            raise ValueError("cross-family degradation is forbidden: oci → local")
        if self.local_engine is None:
            raise ValueError("local engine required when effective backend is LOCAL")
        if self.oci_engine is not None:
            raise ValueError("oci engine must be None when effective backend is LOCAL")
        expected = {
            Platform.LINUX: LocalSandboxEngine.BWRAP,
            Platform.MACOS: LocalSandboxEngine.SEATBELT,
        }
        if self.local_engine is not expected.get(self.platform):
            raise ValueError(
                f"local engine {self.local_engine.value} does not pair with "
                f"platform {self.platform.value}"
            )

    def _validate_oci(self) -> None:
        if self.requested is not SandboxBackend.OCI:
            raise ValueError(
                "cross-family degradation is forbidden: "
                f"{self.requested.value} → oci"
            )
        if self.oci_engine is None:
            raise ValueError("oci engine required when effective backend is OCI")
        if self.local_engine is not None:
            raise ValueError("local engine must be None when effective backend is OCI")

    def _validate_host(self) -> None:
        if self.local_engine is not None or self.oci_engine is not None:
            raise ValueError("host carries no concrete engine")

    def _validate_reason_discipline(self) -> None:
        degraded = self.effective is SandboxBackend.HOST and (
            self.requested is not SandboxBackend.HOST
        )
        if degraded and self.degraded_reason is None:
            raise ValueError(
                "degraded_reason is required on every degradation step — "
                "honest reporting is an obligation"
            )
        if not degraded and self.degraded_reason is not None:
            raise ValueError(
                "degraded_reason must be None when the requested tier is honored"
            )


def _unavailable(engine: str, detail: str) -> str:
    return f"{engine} unavailable: {detail}"


# Windows currently has no local-family implementation.
# The reason must be actionable: name the declared alternatives.
_WINDOWS_NO_LOCAL_REASON = (
    "no local-family engine on windows — declare backend: oci for a "
    "container sandbox or backend: host for native execution"
)


async def _resolve_local(requested: SandboxBackend) -> SandboxSelection:
    platform = _get_platform()
    if platform is Platform.LINUX:
        probe = await probe_bwrap()
        if probe.available:
            return SandboxSelection(
                requested=requested,
                effective=SandboxBackend.LOCAL,
                platform=platform,
                local_engine=LocalSandboxEngine.BWRAP,
            )
        return SandboxSelection(
            requested=requested,
            effective=SandboxBackend.HOST,
            platform=platform,
            degraded_reason=_unavailable("bwrap", probe.detail),
        )
    if platform is Platform.MACOS:
        probe = await probe_seatbelt()
        if probe.available:
            return SandboxSelection(
                requested=requested,
                effective=SandboxBackend.LOCAL,
                platform=platform,
                local_engine=LocalSandboxEngine.SEATBELT,
            )
        return SandboxSelection(
            requested=requested,
            effective=SandboxBackend.HOST,
            platform=platform,
            degraded_reason=_unavailable("sandbox-exec", probe.detail),
        )
    reason = (
        _WINDOWS_NO_LOCAL_REASON
        if platform is Platform.WINDOWS
        else f"no local-family engine on platform {platform.value}"
    )
    return SandboxSelection(
        requested=requested,
        effective=SandboxBackend.HOST,
        platform=platform,
        degraded_reason=reason,
    )


async def _resolve_oci() -> SandboxSelection:
    platform = _get_platform()
    docker_probe = await probe_docker()
    if docker_probe.available:
        return SandboxSelection(
            requested=SandboxBackend.OCI,
            effective=SandboxBackend.OCI,
            platform=platform,
            oci_engine=OciEngine.DOCKER,
        )
    podman_probe = await probe_podman()
    if podman_probe.available:
        return SandboxSelection(
            requested=SandboxBackend.OCI,
            effective=SandboxBackend.OCI,
            platform=platform,
            oci_engine=OciEngine.PODMAN,
        )
    reason = (
        _unavailable("docker", docker_probe.detail)
        + "; "
        + _unavailable("podman", podman_probe.detail)
    )
    return SandboxSelection(
        requested=SandboxBackend.OCI,
        effective=SandboxBackend.HOST,
        platform=platform,
        degraded_reason=reason,
    )


async def resolve_selection(backend: SandboxBackend) -> SandboxSelection:
    """Resolve a configured tier to the typed concrete selection (probing owner).

    ``DEFAULT`` never reaches here (program error). AUTO and
    LOCAL never probe the oci family; OCI never selects LOCAL.
    """
    match backend:
        case SandboxBackend.DEFAULT:
            raise ValueError(
                "backend == DEFAULT must never reach selection; the "
                "assembly layer keeps the sandbox dormant instead"
            )
        case SandboxBackend.HOST:
            return SandboxSelection(
                requested=SandboxBackend.HOST,
                effective=SandboxBackend.HOST,
                platform=_get_platform(),
            )
        case SandboxBackend.AUTO | SandboxBackend.LOCAL:
            return await _resolve_local(backend)
        case SandboxBackend.OCI:
            return await _resolve_oci()


def select_runtime(selection: SandboxSelection) -> SandboxRuntime:
    """Build the concrete runtime for an already-resolved selection.

    No probing, no defaults — the selection's typed fields drive an
    exhaustive dispatch. HostRuntime carries the degradation reason so it
    lands in ``ResolvedSandbox.degraded_reason`` honestly.
    """
    from .runtime import HostRuntime

    match selection.effective:
        case SandboxBackend.LOCAL:
            match selection.local_engine:
                case LocalSandboxEngine.BWRAP:
                    from .bwrap_runtime import BwrapRuntime

                    return BwrapRuntime()
                case LocalSandboxEngine.SEATBELT:
                    from .seatbelt_runtime import SeatbeltRuntime

                    return SeatbeltRuntime()
                case None:
                    raise ValueError("local engine missing on effective LOCAL")
        case SandboxBackend.OCI:
            from .oci_runtime import OciContainerRuntime

            match selection.oci_engine:
                case OciEngine.DOCKER | OciEngine.PODMAN as engine:
                    return OciContainerRuntime(engine=engine)
                case None:
                    raise ValueError("oci engine missing on effective OCI")
        case SandboxBackend.HOST:
            return HostRuntime(degraded_reason=selection.degraded_reason)
        case SandboxBackend.AUTO | SandboxBackend.DEFAULT:
            raise ValueError("AUTO/DEFAULT are not effective tiers")
