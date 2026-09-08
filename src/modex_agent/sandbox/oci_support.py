"""OCI mount compilation, CLI execution/results, and inspect parsing.

Owns path transformation, config fingerprints, and container naming.
Canonical path resolution may touch the filesystem; the CLI runner executes
engine processes. Lifecycle decisions belong to ``oci_lifecycle.py`` and the
resolved substrate contract to ``oci_runtime.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import assert_never

from pydantic import BaseModel, ConfigDict

from modex_agent.workspace.boundary import canonicalize_path

from .exceptions import SandboxConfigurationError, SandboxPermissionError
from .settings import SandboxSettings, WriteSurface

__all__ = [
    "CliResult",
    "ContainerMount",
    "windows_host_to_engine",
]

# --- engine + naming constants -------------------------------------------

_DEFAULT_IMAGE = "modex-sandbox:latest"
_CONTAINER_PREFIX = "modex-sbx"
_CONFIG_HASH_LABEL = "modex.sandbox.configHash"
_CONTAINER_UID = "1000:1000"
_PIDS_LIMIT = "256"
# A running container started within this window is "hot" (possibly in
# active use) and must not be rm'd behind the user's back.
_HOT_WINDOW_SECONDS = 5 * 60
_CLI_TIMEOUT_SECONDS = 30.0
_CREATE_TIMEOUT_SECONDS = 120.0


class CliResult(BaseModel):
    """Outcome of a completed CLI invocation or expected availability failure.

    ``returncode`` is ``None`` when the CLI could not run at all or timed
    out (``error`` carries the reason). Permission and unexpected launcher
    errors propagate; ``unavailable_reason`` can also raise typed errors.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def unavailable_reason(self) -> str:
        """Classify initialization failure, never an executed tool result."""
        detail = (self.error or self.stderr).strip()
        lowered = detail.lower()
        if any(marker in lowered for marker in (
            "permission denied", "operation not permitted", "access denied", "unauthorized",
        )):
            raise SandboxPermissionError(detail)
        if self.error is not None or any(marker in lowered for marker in (
            "cannot connect", "connection refused", "connection reset", "daemon is not running",
            "is the docker daemon running", "image not found", "no such image",
            "manifest unknown", "no matching manifest", "not supported", "no space left",
        )):
            return detail
        raise SandboxConfigurationError(detail or f"engine exited with code {self.returncode}")


async def _run_cli(argv: list[str], timeout: float = _CLI_TIMEOUT_SECONDS) -> CliResult:
    """Run one engine CLI command. Test seam — unit tests monkeypatch this."""

    def _exec() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )

    loop = asyncio.get_running_loop()
    try:
        proc = await loop.run_in_executor(None, _exec)
    except subprocess.TimeoutExpired:
        return CliResult(
            returncode=None, error=f"{argv[0]} timed out after {timeout}s"
        )
    except (FileNotFoundError, ConnectionError, TimeoutError) as exc:
        return CliResult(returncode=None, error=f"{argv[0]} failed: {exc}")
    return CliResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


# --- Windows host path → engine mount-source transform ---------------------

_WINDOWS_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


def windows_host_to_engine(host_path: Path) -> str:
    """``F:\\x\\y`` → ``//f/x/y`` (docker-desktop drive-share syntax)."""
    raw = str(host_path)
    match = _WINDOWS_DRIVE.match(raw)
    if match is None:
        return raw.replace("\\", "/")
    drive, rest = match.groups()
    return f"//{drive.lower()}/{(rest.replace('\\', '/'))}"


class ContainerMount(BaseModel):
    """One compiled ``-v`` mount: host location, in-container path, mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_path: Path
    sandbox_path: str
    read_only: bool = False

    @classmethod
    def for_path(cls, host_path: Path) -> ContainerMount:
        """Same-path mount for *host_path* (POSIX verbatim; Windows drive →
        engine source ``//f/x``, in-container path ``/f/x``)."""
        raw = str(host_path)
        if _WINDOWS_DRIVE.match(raw) is None:
            # The container side uses forward slashes. Invalid engine paths
            # are configuration errors, not permission for HOST fallback.
            return cls(host_path=host_path, sandbox_path=raw.replace("\\", "/"))
        source = windows_host_to_engine(host_path)
        return cls(host_path=host_path, sandbox_path="/" + source.lstrip("/"))

    @property
    def host_source(self) -> str:
        """The CLI ``-v`` source side (engine-drive syntax on Windows hosts;
        POSIX roots keep forward slashes even when parsed on Windows)."""
        raw = str(self.host_path)
        if _WINDOWS_DRIVE.match(raw) is not None:
            return windows_host_to_engine(self.host_path)
        return raw.replace("\\", "/")

    def to_flag(self) -> str:
        mode = "ro" if self.read_only else "rw"
        return f"{self.host_source}:{self.sandbox_path}:{mode}"


def _sandbox_mounts(
    settings: SandboxSettings, workspace_root: Path
) -> list[ContainerMount]:
    """Compile the policy into the ordered mount list.

    ``none``: workspace mounted ``ro`` (no shadows needed — nothing is
    writable). ``workspace``: workspace ``rw`` with each
    ``protected_subpath`` shadowed ``ro`` after it, then each extra
    ``writable_root`` the same way. ``roots``: workspace stays ``ro``
    and only the declared roots mount ``rw`` (with their shadows).
    Later mounts shadow earlier ones at the same destination — the
    ordering IS the read-only-subpath enforcement.
    """
    surface = settings.exclusive.write_surface
    match surface:
        case WriteSurface.NONE:
            return [ContainerMount.for_path(workspace_root).model_copy(update={"read_only": True})]
        case WriteSurface.ROOTS:
            mounts: list[ContainerMount] = []
            for root in settings.exclusive.writable_roots:
                anchored = canonicalize_path(root, base=workspace_root)
                mounts.append(ContainerMount.for_path(anchored))
                for sub in settings.exclusive.protected_subpaths:
                    shadow = ContainerMount.for_path(canonicalize_path(sub, base=anchored))
                    mounts.append(shadow.model_copy(update={"read_only": True}))
            return mounts
        case WriteSurface.WORKSPACE | WriteSurface.FULL:
            mounts = [ContainerMount.for_path(workspace_root)]
            for sub in settings.exclusive.protected_subpaths:
                shadow = ContainerMount.for_path(canonicalize_path(sub, base=workspace_root))
                mounts.append(shadow.model_copy(update={"read_only": True}))
            for root in settings.exclusive.writable_roots:
                anchored = canonicalize_path(root, base=workspace_root)
                mounts.append(ContainerMount.for_path(anchored))
                for sub in settings.exclusive.protected_subpaths:
                    shadow = ContainerMount.for_path(canonicalize_path(sub, base=anchored))
                    mounts.append(shadow.model_copy(update={"read_only": True}))
            return mounts
        case unreachable:
            assert_never(unreachable)


def _config_hash(
    engine: str,
    image: str,
    network: bool,
    mounts: list[ContainerMount],
) -> str:
    """Stable config fingerprint — the label that gates container reuse."""
    digest = hashlib.sha256()
    digest.update(engine.encode())
    digest.update(b"\0")
    digest.update(image.encode())
    digest.update(b"\0")
    digest.update(b"network=" + (b"1" if network else b"0"))
    for mount in mounts:
        digest.update(b"\0")
        digest.update(mount.to_flag().encode())
    return digest.hexdigest()[:16]


def _container_name(workspace_root: Path) -> str:
    """``modex-sbx-<pool_slug>`` — deterministic per workspace."""
    slug = re.sub(r"[^a-z0-9-]+", "-", str(workspace_root).lower()).strip("-")
    return f"{_CONTAINER_PREFIX}-{slug}"


# --- inspect parsing --------------------------------------------------------


class _ContainerFacts(BaseModel):
    """The three facts the lifecycle decision needs from ``inspect``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    running: bool
    started_at: float
    config_hash: str | None


_EPOCH_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?")


def _parse_started_at(raw: str) -> float | None:
    """Docker emits ``2026-09-04T12:00:00.123456789Z`` (ns precision, which
    ``datetime.fromisoformat`` rejects on Python < 3.11 — trim manually)."""
    match = _EPOCH_RE.match(raw)
    if match is None:
        return None
    base, clock, frac = match.groups()
    micros = (frac or ".0")[:7]
    try:
        dt = datetime.fromisoformat(f"{base}T{clock}{micros}+00:00")
    except ValueError:
        return None
    return dt.timestamp()


def _parse_inspect(stdout: str) -> _ContainerFacts | None:
    """Parse inspect facts; return None for recognized malformed output.

    Unexpected shapes or model validation failures can still raise.
    """
    try:
        data = json.loads(stdout)
        entry = data[0]
        state = entry["State"]
        labels = entry["Config"].get("Labels") or {}
        started = _parse_started_at(str(state["StartedAt"]))
        if started is None:
            return None
        return _ContainerFacts(
            running=bool(state["Running"]),
            started_at=started,
            config_hash=labels.get(_CONFIG_HASH_LABEL),
        )
    except (json.JSONDecodeError, IndexError, KeyError, TypeError):
        return None
