"""OCI container lifecycle and resolved shell launchers.

CLI-driven container lifecycle for the oci family (docker ∥ podman — two
engine names over one CLI-compatible surface, selected by the typed
``OciEngine``; NOT two backends). The engine is chosen by the selector
(``selection.resolve_selection``) from engine availability results; this
runtime additionally checks workspace mount visibility before shell binding.

Siblings: ``oci_support.py`` owns compilation and CLI support (mounts,
config hash, naming, inspect parsing, the CLI runner); ``oci_lifecycle.py``
owns the imperative container half (ensure/reuse/rebuild/create, the
consistency probe). This module owns the resolve contract — turning an
ensured container into the ``ResolvedSandbox`` argv substrate.

Lifecycle facts: container ``modex-sbx-<workspace_slug>``, configHash-label
reuse (running or unknown-owner mismatch raises; stopped owned mismatch
rebuilds), creation serialized per engine/name, hardening flags on
create (``--read-only --tmpfs /tmp --cap-drop ALL --security-opt
no-new-privileges --pids-limit 256 --user 1000:1000``), and a workspace
mount-consistency probe. Host environment is inherited by engine CLI
processes, not injected into container commands. Confirmed initialization
unavailability permits reported HOST fallback; permission and configuration
errors propagate. No possibly-submitted agent command is replayed. The probe
checks workspace visibility/deletion, not every mount's permissions or tool.
CLI execution uses argument arrays, not joined host-shell commands.
"""

from __future__ import annotations

from pathlib import Path

from .oci_lifecycle import ContainerLifecycle
from .oci_support import (
    _CLI_TIMEOUT_SECONDS,
    _DEFAULT_IMAGE,
    CliResult,  # noqa: F401 — re-exported seam (tests patch oci_runtime._run_cli)
    _config_hash,
    _container_name,
    _run_cli,
    _sandbox_mounts,
)
from .oci_support import ContainerMount as ContainerMount
from .oci_support import windows_host_to_engine as windows_host_to_engine
from .runtime import HostRuntime, MountEntry, ResolvedSandbox, SandboxRuntime
from .selection import OciEngine
from .settings import SandboxBackend, SandboxSettings
from .types import EnforcementLevel

__all__ = [
    "CliResult",
    "ContainerMount",
    "OciContainerRuntime",
    "windows_host_to_engine",
]

# In-container shell argv tail — identical to HostRuntime's (the marker
# protocol needs deterministic no-profile interactive bash).
_BASH_PATH = "/bin/bash"
_BASH_SPAWN_ARGS = ["--noprofile", "--norc", "-i"]


async def _cli_runner(
    argv: list[str], timeout: float = _CLI_TIMEOUT_SECONDS
) -> CliResult:
    """Late-binding wrapper: resolves ``_run_cli`` from THIS module's
    namespace at call time so the test seam (monkeypatching
    ``oci_runtime._run_cli``) stays live."""
    return await _run_cli(argv, timeout=timeout)


class OciContainerRuntime(SandboxRuntime):
    """oci-family runtime: CLI container lifecycle + argv compilation.

    ``engine`` is the typed ``OciEngine`` (DOCKER / PODMAN) chosen by the
    selector from the probe chain — resolve() does not re-probe.
    """

    def __init__(self, engine: OciEngine = OciEngine.DOCKER) -> None:
        self._engine = engine
        self._lifecycle = ContainerLifecycle(engine.value, _cli_runner)

    @property
    def engine(self) -> OciEngine:
        """The typed engine this runtime drives."""
        return self._engine

    async def resolve(
        self, settings: SandboxSettings, workspace_root: Path
    ) -> ResolvedSandbox:
        image = settings.image or _DEFAULT_IMAGE
        mounts = _sandbox_mounts(settings, workspace_root)
        config_hash = _config_hash(self._engine.value, image, settings.network, mounts)
        name = _container_name(workspace_root)

        outcome = await self._lifecycle.ensure_container(
            name, config_hash, image, mounts, settings
        )
        if outcome.container is None:
            return await self._host_degraded(settings, workspace_root, outcome.reason)

        probe_error = await self._lifecycle.probe_mount_consistency(
            outcome.container, workspace_root
        )
        if probe_error is not None:
            return await self._host_degraded(settings, workspace_root, probe_error)

        ws_mount = ContainerMount.for_path(workspace_root)
        shell_argv = [
            self._engine.value,
            "exec",
            "-it",
            "-w",
            ws_mount.sandbox_path,
            outcome.container,
            _BASH_PATH,
            *_BASH_SPAWN_ARGS,
        ]
        one_shot = [self._engine.value, "exec", outcome.container]
        mount_table = [
            MountEntry(
                host_path=m.host_path,
                sandbox_path=Path(m.sandbox_path),
                read_only=m.read_only,
            )
            for m in mounts
        ]
        return ResolvedSandbox(
            backend=SandboxBackend.OCI,
            enforcement=EnforcementLevel.FULL,
            shell_argv=shell_argv,
            one_shot_command_argv_prefix=one_shot,
            mount_table=mount_table,
        )

    async def _host_degraded(
        self,
        settings: SandboxSettings,
        workspace_root: Path,
        reason: str | None,
    ) -> ResolvedSandbox:
        """Honest degrade: the host-shaped result with the reason attached."""
        host = await HostRuntime().resolve(settings, workspace_root)
        return host.model_copy(update={"degraded_reason": reason})
