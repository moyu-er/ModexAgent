"""Cached CLI availability probes for the sandbox execution substrate.

Existence + version checks for the four engine CLIs:

- ``bwrap`` / ``sandbox-exec`` — local family (host view)
- ``docker`` / ``podman`` — oci family (two engine names for the same CLI-
  compatible surface; they are one implementation, not two backends)

Probes are async and cache availability results per process. Expected engine
unavailability is reported as ``ProbeResult.available=False`` with detail.
OS executable-permission errors propagate: they do not authorize a HOST
fallback. Family selection belongs to the selector, not these probes.

CLI subprocesses always run off the event loop via ``anyio.to_thread``
(the blocking call would otherwise starve the loop for the whole timeout
window). ``bwrap`` additionally runs a real sandbox smoke after the version
check — a version string proves the binary exists, not that user namespaces
and mounts actually work on this host (AppArmor-restricted userns on Ubuntu
24.04+ otherwise yields false FULL enforcement).
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Awaitable, Callable
from typing import Final

import anyio.to_thread
from pydantic import BaseModel, ConfigDict

# How long a version check may run before the engine counts as unavailable.
_VERSION_CHECK_TIMEOUT_SECONDS: Final[float] = 10.0

# How long the bwrap sandbox smoke may run before the engine counts as
# unavailable (a working bwrap smoke completes in milliseconds).
_SMOKE_TIMEOUT_SECONDS: Final[float] = 10.0

# Minimal sandbox smoke argv: the same mount primitives BwrapRuntime compiles
# (ro-bind /, fresh /tmp, minimal /dev, fresh /proc) around a trivial exit-0
# command. Exercises userns + mount setup, not just argument parsing.
_BWRAP_SMOKE_ARGV: Final[list[str]] = [
    "bwrap",
    "--ro-bind",
    "/",
    "/",
    "--tmpfs",
    "/tmp",
    "--dev",
    "/dev",
    "--proc",
    "/proc",
    "--die-with-parent",
    "--",
    "/bin/true",
]

# stderr signatures of unprivileged-userns denial (Ubuntu 24.04+ AppArmor
# restriction and classic EPERM). A smoke failing with one of these is a
# host-policy fact the detail string must make actionable.
_USERNS_DENIAL_SIGNATURES: Final[tuple[str, ...]] = (
    "setting up uid map",
    "creating new namespace",
    "operation not permitted",
    "permission denied",
)

# Actionable remedy for the userns-denial signatures above.
_USERNS_DENIAL_HINT: Final[str] = (
    "unprivileged user namespaces appear restricted "
    "(try: sysctl kernel.apparmor_restrict_unprivileged_userns=0)"
)


def _which(name: str) -> str | None:
    """Seam for tests — resolves an executable like shutil.which."""
    return shutil.which(name)


def _exec_cli(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run one CLI call to completion (blocking) — offloaded to a worker thread."""
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


async def _run_version(argv: list[str]) -> tuple[int, str]:
    """Seam for tests — runs ``argv`` and returns ``(returncode, stdout)``."""
    returncode, stdout, _ = await anyio.to_thread.run_sync(
        lambda: _exec_cli(argv, _VERSION_CHECK_TIMEOUT_SECONDS)
    )
    return returncode, stdout


async def _run_smoke(argv: list[str]) -> tuple[int, str]:
    """Seam for tests — runs the sandbox smoke; returns ``(returncode, stderr)``."""
    returncode, _, stderr = await anyio.to_thread.run_sync(
        lambda: _exec_cli(argv, _SMOKE_TIMEOUT_SECONDS)
    )
    return returncode, stderr


class ProbeResult(BaseModel):
    """Availability fact consumed by the platform-aware runtime selector."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    available: bool
    detail: str


# Probe results persist until explicitly cleared; later startup can still fail.
_probe_cache: dict[str, ProbeResult] = {}


def clear_probe_cache() -> None:
    """Reset cached probe results (test seam; also after platform changes)."""
    _probe_cache.clear()


async def _probe_cli(
    name: str,
    executable: str,
    version_argv: list[str],
) -> ProbeResult:
    """Probe one CLI: which() → version check (no cache — callers own caching)."""
    path = _which(executable)
    if path is None:
        return ProbeResult(available=False, detail=f"{executable}: not found on PATH")

    try:
        returncode, stdout = await _run_version(version_argv)
    except subprocess.TimeoutExpired:
        return ProbeResult(
            available=False,
            detail=f"{executable}: version check timed out "
            f"(> {_VERSION_CHECK_TIMEOUT_SECONDS:.0f}s)",
        )
    except PermissionError:
        # Execution permission is not absence of an engine. Do not let the
        # selector turn an OS launch refusal into permission to use HOST.
        raise
    except OSError as exc:
        return ProbeResult(available=False, detail=f"{executable}: version check failed: {exc}")

    first_line = stdout.splitlines()[0] if stdout else ""
    if returncode == 0:
        return ProbeResult(available=True, detail=first_line or f"{path}: ok")
    return ProbeResult(
        available=False,
        detail=f"{executable}: version check exit {returncode}: {first_line}",
    )


async def _probe_bwrap_with_smoke() -> ProbeResult:
    """bwrap probe: version check, then a real sandbox smoke.

    The version check proves the binary exists and parses args; the smoke
    checks the minimal user-namespace and mount setup on this host. It does
    not validate every configured mount or guard. The smoke is skipped when
    the version check failed; launcher PermissionError propagates.
    """
    version = await _probe_cli("bwrap", "bwrap", ["bwrap", "--version"])
    if not version.available:
        return version

    try:
        returncode, stderr = await _run_smoke(_BWRAP_SMOKE_ARGV)
    except subprocess.TimeoutExpired:
        return ProbeResult(
            available=False,
            detail=f"bwrap: sandbox smoke timed out (> {_SMOKE_TIMEOUT_SECONDS:.0f}s)",
        )
    except PermissionError:
        raise
    except OSError as exc:
        return ProbeResult(available=False, detail=f"bwrap: sandbox smoke failed: {exc}")

    if returncode == 0:
        return version

    detail = f"bwrap: sandbox smoke exit {returncode}: {stderr or 'no stderr'}"
    if any(sig in stderr.lower() for sig in _USERNS_DENIAL_SIGNATURES):
        detail = f"{detail} — {_USERNS_DENIAL_HINT}"
    return ProbeResult(available=False, detail=detail)


async def _probe_cached(
    name: str,
    probe: Callable[[], Awaitable[ProbeResult]],
) -> ProbeResult:
    """Cache one probe result under ``name`` for the process lifetime."""
    cached = _probe_cache.get(name)
    if cached is not None:
        return cached
    result = await probe()
    _probe_cache[name] = result
    return result


async def probe_bwrap() -> ProbeResult:
    """Probe the Linux local-family engine (bubblewrap): version + sandbox smoke."""
    return await _probe_cached("bwrap", _probe_bwrap_with_smoke)


async def probe_seatbelt() -> ProbeResult:
    """Probe the macOS local-family engine (sandbox-exec).

    ``sandbox-exec`` has no version flag; the smoke check compiles a trivial
    no-op profile (``(version 1)(allow default)``) with ``-p`` and ``-n``
    (dry-run) — exit 0 means the binary runs and accepts profiles.
    """
    return await _probe_cached(
        "seatbelt",
        lambda: _probe_cli(
            "seatbelt", "sandbox-exec", ["sandbox-exec", "-n", "-p", "(version 1)(allow default)"]
        ),
    )


async def probe_docker() -> ProbeResult:
    """Probe the oci-family docker engine."""
    return await _probe_cached(
        "docker", lambda: _probe_cli("docker", "docker", ["docker", "--version"])
    )


async def probe_podman() -> ProbeResult:
    """Probe the oci-family podman engine."""
    return await _probe_cached(
        "podman", lambda: _probe_cli("podman", "podman", ["podman", "--version"])
    )


__all__ = [
    "ProbeResult",
    "clear_probe_cache",
    "probe_bwrap",
    "probe_docker",
    "probe_podman",
    "probe_seatbelt",
]
