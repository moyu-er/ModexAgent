"""Real-bwrap integration tests for BwrapRuntime (Ticket 05).

These exercise the *actual* enforcement — the compiled argv runs through a
real bubblewrap on Linux with unprivileged userns available. On any host
without ``bwrap`` (CI, Windows, macOS) every test skips via the module-level
``skipif``; the parameter-compilation contract itself is platform-free and
lives in ``test_bwrap_runtime.py``.

Covers the PRD P1 acceptance items for Linux + bwrap:

- persistent shell end-to-end: marker protocol through ``pexpect`` on the
  bwrap-wrapped bash (echo a value, read it back), cwd continuity (``cd``
  then ``pwd`` reports the moved directory)
- write boundary: WORKSPACE_WRITE allows writing inside the workspace,
  denies ``/etc`` with "Read-only file system"
- network isolation: ``--unshare-net`` breaks socket connect (``gaierror``
  — no DNS inside the namespace)
- ``.git`` read-only: the ro-bind shadow denies writes under
  ``<ws>/.git`` while the workspace stays writable
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from modex_agent.sandbox.bwrap_runtime import BwrapRuntime
from modex_agent.sandbox.settings import (
    SandboxBackend,
    SandboxPolicy,
    SandboxSettings,
)
from modex_agent.sandbox.types import EnforcementLevel

pytest.importorskip("pexpect", reason="persistent-shell integration needs pexpect")

_HAS_BWRAP = shutil.which("bwrap") is not None and sys.platform.startswith("linux")

pytestmark = pytest.mark.skipif(not _HAS_BWRAP, reason="bwrap not available")


async def _resolve_write_policy(workspace: Path) -> list[str]:
    settings = SandboxSettings(
        backend=SandboxBackend.LOCAL,
        policy=SandboxPolicy.WORKSPACE_WRITE,
        network=False,
    )
    resolved = await BwrapRuntime().resolve(settings, workspace)
    assert resolved.backend is SandboxBackend.LOCAL
    assert resolved.enforcement is EnforcementLevel.FULL
    return resolved.shell_argv


# ---------------------------------------------------------------------------
# Persistent shell end-to-end through the T04 seam
# ---------------------------------------------------------------------------


class TestPersistentShellEndToEnd:
    async def test_marker_protocol_runs_inside_bwrap(self, tmp_path: Path) -> None:
        """A real session on the bwrap argv answers a command and returns
        its output — the marker protocol works unchanged through the
        namespace wrapper (PRD: execve 型 CLI 包装)."""
        from modex_agent.tools.terminal._persistent_session import PersistentShellSession

        shell_argv = await _resolve_write_policy(tmp_path)
        session = PersistentShellSession(shell_argv=shell_argv, timeout_seconds=30)
        try:
            out = await session.run_command("echo bwrap-marker-ok")
            assert out.strip() == "bwrap-marker-ok"
        finally:
            await session.close()

    async def test_cwd_persists_across_commands(self, tmp_path: Path) -> None:
        """``cd`` inside the sandboxed shell sticks: the next command sees
        the moved cwd (the persistent-session contract, held through bwrap)."""
        from modex_agent.tools.terminal._persistent_session import PersistentShellSession

        ws = tmp_path / "ws"
        ws.mkdir()
        shell_argv = await _resolve_write_policy(tmp_path)
        session = PersistentShellSession(shell_argv=shell_argv, timeout_seconds=30)
        try:
            await session.run_command(f"cd {ws}")
            out = await session.run_command("pwd")
            assert out.strip() == str(ws)
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Write boundary
# ---------------------------------------------------------------------------


class TestWriteBoundary:
    async def test_workspace_write_allowed(self, tmp_path: Path) -> None:
        from modex_agent.tools.terminal._persistent_session import PersistentShellSession

        marker = tmp_path / "boundary.txt"
        shell_argv = await _resolve_write_policy(tmp_path)
        session = PersistentShellSession(shell_argv=shell_argv, timeout_seconds=30)
        try:
            session_out = await session.run_command(f"touch {marker} && echo WROTE")
            assert "WROTE" in session_out
        finally:
            await session.close()
        assert marker.exists()

    async def test_etc_write_denied_read_only(self, tmp_path: Path) -> None:
        """Root is ro-bound: /etc writes fail with the actionable
        "Read-only file system" denial the interceptor layer translates."""
        from modex_agent.tools.terminal._persistent_session import PersistentShellSession

        shell_argv = await _resolve_write_policy(tmp_path)
        session = PersistentShellSession(shell_argv=shell_argv, timeout_seconds=30)
        try:
            out = await session.run_command("touch /etc/bwrap-denied 2>&1; echo rc=$?")
            assert "Read-only file system" in out
            assert "rc=1" in out
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Network isolation
# ---------------------------------------------------------------------------


class TestNetworkIsolation:
    async def test_socket_connect_fails_when_unshared(self, tmp_path: Path) -> None:
        """``--unshare-net``: DNS resolution itself fails inside the
        namespace — a python socket connect reports gaierror, never a
        successful connection."""
        from modex_agent.tools.terminal._persistent_session import PersistentShellSession

        shell_argv = await _resolve_write_policy(tmp_path)
        session = PersistentShellSession(shell_argv=shell_argv, timeout_seconds=60)
        try:
            out = await session.run_command(
                "python3 - <<'EOF'\n"
                "import socket\n"
                "try:\n"
                "    socket.create_connection(('example.com', 80), timeout=3)\n"
                "    print('NET_LEAK')\n"
                "except OSError as exc:\n"
                "    print('NET_BLOCKED:', type(exc).__name__)\n"
                "EOF"
            )
            assert "NET_BLOCKED:" in out
            assert "NET_LEAK" not in out
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# protected_subpaths: .git read-only inside a writable workspace
# ---------------------------------------------------------------------------


class TestProtectedSubpaths:
    async def test_git_dir_write_denied(self, tmp_path: Path) -> None:
        from modex_agent.tools.terminal._persistent_session import PersistentShellSession

        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        shell_argv = await _resolve_write_policy(tmp_path)
        session = PersistentShellSession(shell_argv=shell_argv, timeout_seconds=30)
        try:
            out = await session.run_command(f"touch {git_dir}/hooks_x 2>&1; echo rc=$?")
            assert "Read-only file system" in out
            assert "rc=1" in out
            # The same session can still write the workspace itself — the
            # shadow is scoped to .git, not the whole root.
            ok = await session.run_command(f"touch {tmp_path}/ok.txt && echo STILL_WRITABLE")
            assert "STILL_WRITABLE" in ok
        finally:
            await session.close()
