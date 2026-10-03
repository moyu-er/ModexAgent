"""oci container lifecycle — inspect/reuse/rebuild/create + consistency probe.

Owns the ensure-container decision (inspect ->
reuse / start / rm+rebuild / create, serialized per name), the hardened
create argv, and the mount-consistency probe. Pure helpers (mounts, hash,
naming, inspect parsing) live in ``oci_support.py``; the resolve contract
lives in ``oci_runtime.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict

from .exceptions import SandboxConfigurationError
from .oci_support import (
    _CONFIG_HASH_LABEL,
    _CONTAINER_UID,
    _CREATE_TIMEOUT_SECONDS,
    _PIDS_LIMIT,
    CliResult,
    ContainerMount,
    _ContainerFacts,
    _parse_inspect,
)
from .settings import SandboxSettings

__all__ = ["CliRunner", "ContainerLifecycle", "EnsureOutcome"]

# Engine CLI seam: expected availability failures return CliResult; others propagate.
CliRunner = Callable[..., Awaitable[CliResult]]
_CONTAINER_LOCKS: WeakValueDictionary[tuple[str, str], asyncio.Lock] = WeakValueDictionary()


class EnsureOutcome(BaseModel):
    """Result of the ensure-container step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    container: str | None  # None → degraded (reason below)
    reason: str | None = None


class ContainerLifecycle:
    """The per-container CLI decision flow, driven over an injected runner.

    ``run`` is the engine-CLI seam (``oci_runtime._run_cli`` in
    production, the FakeCli stand-in in tests); ``engine`` is the CLI
    program name (``OciEngine.value``). Creation is serialized per
    container name — the dict-of-locks is the whole mechanism.
    """

    def __init__(self, engine: str, run: CliRunner) -> None:
        self._engine = engine
        self._run = run

    async def ensure_container(
        self,
        name: str,
        config_hash: str,
        image: str,
        mounts: list[ContainerMount],
        settings: SandboxSettings,
    ) -> EnsureOutcome:
        """inspect → reuse / start / rm+rebuild / create, serialized per name."""
        lock = _CONTAINER_LOCKS.setdefault((self._engine, name), asyncio.Lock())
        async with lock:
            inspect = await self._run([self._engine, "inspect", name])
            if inspect.error is not None:
                return EnsureOutcome(
                    container=None, reason=f"inspect {name}: {inspect.unavailable_reason()}"
                )
            if not inspect.ok and "no such" not in inspect.stderr.lower():
                return EnsureOutcome(container=None, reason=f"inspect {name}: {inspect.unavailable_reason()}")
            if inspect.ok:
                facts = _parse_inspect(inspect.stdout)
                if facts is None:
                    return EnsureOutcome(
                        container=None,
                        reason=f"inspect {name}: unparseable output",
                    )
                if facts.config_hash == config_hash:
                    return await self._reuse_matching(name, facts)
                mismatch = await self._rebuild_mismatched(name, facts)
                if mismatch is not None:
                    return mismatch
            create = self.create_argv(name, config_hash, image, mounts, settings)
            run = await self._run(create, timeout=_CREATE_TIMEOUT_SECONDS)
            if not run.ok:
                return EnsureOutcome(
                    container=None,
                    reason=f"create {name} failed: "
                     f"{run.unavailable_reason()}",
                )
            return EnsureOutcome(container=name)

    async def _reuse_matching(
        self, name: str, facts: _ContainerFacts
    ) -> EnsureOutcome:
        """Hash-matching leg: running → reuse; stopped → start."""
        if facts.running:
            return EnsureOutcome(container=name)
        start = await self._run([self._engine, "start", name])
        if not start.ok:
            return EnsureOutcome(
                container=None,
                reason=f"start {name} failed: {start.unavailable_reason()}",
            )
        return EnsureOutcome(container=name)

    async def _rebuild_mismatched(
        self, name: str, facts: _ContainerFacts
    ) -> EnsureOutcome | None:
        """Hash-mismatch leg: hot → honest recreate-required error (never rm
        a possibly-in-use container); cold → rm -f and return None so the
        caller rebuilds."""
        if facts.running or facts.config_hash is None:
            raise SandboxConfigurationError(
                f"container {name} config changed or ownership is unknown; "
                "recreate required after all users have released it"
            )
        rm = await self._run([self._engine, "rm", "-f", name])
        # rm -f of a missing container is rc 1 with "No such object" —
        # treat as success; anything else is real.
        if not rm.ok and "no such" not in rm.stderr.lower():
            return EnsureOutcome(
                container=None,
                reason=f"rm {name} failed: {rm.unavailable_reason()}",
            )
        return None

    def create_argv(
        self,
        name: str,
        config_hash: str,
        image: str,
        mounts: list[ContainerMount],
        settings: SandboxSettings,
    ) -> list[str]:
        """The hardened create argv (argv arrays only — never sh -c)."""
        argv = [
            self._engine,
            "run",
            "-d",
            "--name",
            name,
            "--read-only",
            "--tmpfs",
            "/tmp",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            _PIDS_LIMIT,
            "--user",
            _CONTAINER_UID,
            "--network",
            "bridge" if settings.network else "none",
        ]
        for mount in mounts:
            argv.extend(["-v", mount.to_flag()])
        argv.extend(
            [
                "--label",
                f"{_CONFIG_HASH_LABEL}={config_hash}",
                image,
                "sleep",
                "infinity",
            ]
        )
        return argv

    async def probe_mount_consistency(
        self, container: str, workspace_root: Path
    ) -> str | None:
        """Verify a host-created probe appears and disappears in the container.

        Returns an unavailability reason or None; permission/configuration
        and filesystem errors can propagate. This is not a full mount audit.
        """
        token = uuid.uuid4().hex[:8]
        probe_name = f".modex-sbx-probe-{token}"
        ws_mount = ContainerMount.for_path(workspace_root)
        host_probe = workspace_root / probe_name
        container_probe = f"{ws_mount.sandbox_path}/{probe_name}"
        host_probe.write_text("modex-sbx-probe\n", encoding="utf-8")
        try:
            visible = await self._run(
                [self._engine, "exec", container, "test", "-f", container_probe]
            )
            if not visible.ok:
                if visible.error or visible.stderr:
                    return f"mount-consistency probe failed: {visible.unavailable_reason()}"
                return (
                    f"mount-consistency probe failed: {container_probe} not visible "
                    f"in {container} (drive share / path transform issue)"
                )
            host_probe.unlink(missing_ok=True)
            gone = await self._run(
                [self._engine, "exec", container, "test", "!", "-f", container_probe]
            )
            if not gone.ok:
                if gone.error or gone.stderr:
                    return f"mount-consistency probe failed: {gone.unavailable_reason()}"
                return (
                    f"mount-consistency probe failed: deleting {container_probe} on "
                    f"the host did not remove it in {container}"
                )
            return None
        finally:
            # host-side cleanup even on unexpected paths
            host_probe.unlink(missing_ok=True)
