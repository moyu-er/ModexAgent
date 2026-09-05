"""SandboxRuntime ABC, startup validation, and native HOST runtime.

The Runtime layer never executes agent commands: it resolves the effective execution
substrate and produces a ``ResolvedSandbox`` — the argv-shaped substrate
the three bash implementations consume. Argument-vector compilation
for bwrap / seatbelt / oci lives in their own modules; tier resolution and
concrete runtime selection live in ``selection.py`` (the single probe +
factory owner). ``resolve_available`` validates compiled local launchers with
a constant no-op before shell binding and owns typed initialization fallback.

Tier selection is enforced in ``selection.py``:
``DEFAULT`` is *unconfigured* (never reaches resolution — program error);
``AUTO`` probes only the local family for the platform; AUTO and LOCAL
never fall to Docker/Podman, and OCI never falls to LOCAL. Every
degradation produces a ``degraded_reason`` — enforcement reporting is an
obligation, never a silent reroute.
"""

from __future__ import annotations

import asyncio
import errno
import subprocess
from abc import ABC, abstractmethod
from collections.abc import MutableMapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from modex_agent.workspace.boundary import canonicalize_path

from .exceptions import SandboxConfigurationError, SandboxUnavailableError
from .platform import resolve_shell
from .settings import SandboxBackend, SandboxSettings
from .types import EnforcementLevel

_resolve_host_shell = resolve_shell

__all__ = [
    "HostRuntime",
    "MountEntry",
    "ResolvedSandbox",
    "SandboxEnforcementSnapshot",
    "SandboxRuntime",
    "write_enforcement_snapshot",
]


class MountEntry(BaseModel):
    """One same-path mount: host location visible at the identical in-sandbox path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_path: Path
    sandbox_path: Path
    read_only: bool = False


class ResolvedSandbox(BaseModel):
    """The effective execution substrate after tier resolution + probing.

    ``backend`` is what actually runs commands — one of LOCAL / OCI / HOST.
    ``DEFAULT`` is rejected: it means "unconfigured" and must never survive
    to resolution (the assembly layer keeps the sandbox dormant instead).
    ``enforcement`` describes this substrate, not guard enablement or the
    safety of every mount, tool, or external provider operation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: SandboxBackend
    enforcement: EnforcementLevel
    # Persistent-shell argv (spawn seam). Host with no bash found → [].
    shell_argv: list[str]
    # One-shot command argv prefix (subprocess executor seam). Host → [].
    one_shot_command_argv_prefix: list[str]
    # Mount table (OCI host/container path mapping). Host -> None.
    mount_table: list[MountEntry] | None = None
    # Populated on every degradation step — the honest-reporting obligation.
    degraded_reason: str | None = None

    @field_validator("backend")
    @classmethod
    def _reject_default(cls, value: SandboxBackend) -> SandboxBackend:
        if value is SandboxBackend.DEFAULT:
            raise ValueError(
                "DEFAULT is not a runtime backend — the assembly layer must "
                "keep the sandbox dormant when backend == DEFAULT instead of "
                "resolving it"
            )
        return value


class SandboxEnforcementSnapshot(BaseModel):
    """Telemetry-only record of what enforcement actually took effect.

    Written to turn state under ``TurnCustomKey.SANDBOX_ENFORCEMENT``.
    Consumers are logs/diagnostics only — never approval, never routing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: SandboxBackend
    enforcement: EnforcementLevel
    degraded_reason: str | None = None


def write_enforcement_snapshot(
    state_custom: MutableMapping[str, object],
    resolved: ResolvedSandbox,
) -> None:
    """Record the enforcement snapshot into per-turn custom state.

    ``state_custom`` is ``TurnStateBase.custom`` (``dict[str, Any]``); the
    key is the typed ``TurnCustomKey.SANDBOX_ENFORCEMENT``. The runtime-state
    vocabulary is loaded locally when writing telemetry; this integration
    still depends on the framework runtime contract.
    """
    from modex_agent.runtime.enums import TurnCustomKey

    state_custom[TurnCustomKey.SANDBOX_ENFORCEMENT] = SandboxEnforcementSnapshot(
        backend=resolved.backend,
        enforcement=resolved.enforcement,
        degraded_reason=resolved.degraded_reason,
    )


class SandboxRuntime(ABC):
    """Resolve the effective execution substrate for a workspace.

    Implementations compile launcher arguments and may create profiles or
    containers. ``resolve_available`` canonicalizes roots and validates local
    startup with a constant no-op before shell binding. ``HostRuntime``
    supplies native execution without kernel isolation.
    """

    @abstractmethod
    async def resolve(
        self, settings: SandboxSettings, workspace_root: Path
    ) -> ResolvedSandbox:
        """Resolve the substrate for *workspace_root* under *settings*."""

    async def resolve_available(
        self, settings: SandboxSettings, workspace_root: Path
    ) -> ResolvedSandbox:
        """Resolve before execution, degrading only expected unavailability.

        Permission, configuration and programming errors propagate. This method
        must never wrap command execution: a failed operation is not permission
        to execute it again on the host.
        """
        workspace_root = canonicalize_path(workspace_root)
        settings = settings.model_copy(update={
            "writable_roots": [canonicalize_path(root, base=workspace_root) for root in settings.writable_roots],
        })
        try:
            resolved = await self.resolve(settings, workspace_root)
            await self._validate_startup(resolved, workspace_root)
            return resolved
        except SandboxUnavailableError as exc:
            reason = str(exc)
        except (FileNotFoundError, ConnectionError, TimeoutError) as exc:
            reason = str(exc)
        except OSError as exc:
            if exc.errno not in {
                errno.ENOENT, errno.ENOSPC, errno.ENODEV, errno.ENOSYS,
                errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
            }:
                raise
            reason = str(exc)
        return await HostRuntime(
            degraded_reason=f"sandbox initialization unavailable: {reason}"
        ).resolve(settings, workspace_root)

    async def _validate_startup(self, resolved: ResolvedSandbox, workspace_root: Path) -> None:
        """Concrete runtimes may validate their compiled launch before binding."""

    async def close(self) -> None:
        """Release owned artifacts after all bound shells have closed.

        Shared OCI containers are deliberately not owned by an agent runtime.
        """


async def validate_local_startup(resolved: ResolvedSandbox, workspace_root: Path) -> None:
    """Test the compiled mount/profile argv, not just engine availability.

    Only a constant no-op is sent. A failure here cannot have executed an
    agent command; later operation failures must never use this fallback.
    """
    if resolved.backend is not SandboxBackend.LOCAL:
        return
    if not resolved.shell_argv:
        raise SandboxUnavailableError("local sandbox bash is unavailable")
    argv = [*resolved.shell_argv[:-1], "-c", ":"]
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, cwd=workspace_root, capture_output=True,
            text=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxUnavailableError("local sandbox startup timed out") from exc
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip()
    if any(marker in detail.lower() for marker in (
        "creating new namespace failed", "no permissions to create new namespace",
        "setting up uid map", "setting up gid map", "function not implemented",
        "sandbox_init: operation not permitted",
    )):
        raise SandboxUnavailableError(detail)
    raise SandboxConfigurationError(f"local sandbox startup failed: {detail}")


class HostRuntime(SandboxRuntime):
    """Native execution: no isolation, guard+approval only (enforcement=NONE).

    ``degraded_reason`` is set when the HOST tier is the *result of a
    degradation* decided by the selector — the honest-reporting obligation.
    An explicitly requested HOST tier constructs with ``None``.
    """

    def __init__(self, degraded_reason: str | None = None) -> None:
        self._degraded_reason = degraded_reason

    async def resolve(
        self, settings: SandboxSettings, workspace_root: Path
    ) -> ResolvedSandbox:
        shell = _resolve_host_shell()
        shell_argv = [shell, "--noprofile", "--norc", "-i"] if shell else []
        return ResolvedSandbox(
            backend=SandboxBackend.HOST,
            enforcement=EnforcementLevel.NONE,
            shell_argv=shell_argv,
            one_shot_command_argv_prefix=[],
            degraded_reason=self._degraded_reason,
        )
