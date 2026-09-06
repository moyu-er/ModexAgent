"""Integration tests for OciContainerRuntime + ContainerShellExecutor
(Ticket 07) against a REAL docker engine.

Skipped entirely when no ``docker`` CLI is on PATH (CI, WSL-less Windows).
Run for real inside WSL::

    wsl -d Ubuntu-24.04 -- bash -lc \\
      "cd /mnt/f/tool/pythonProject/ModexAgent && uv run pytest \\
       tests/unit/sandbox/test_oci_integration.py -v -m ''"

Coverage (ticket §验证 + PRD 双 PTY 验证里程碑):

- container reuse: two resolves on one workspace → second hits inspect, no
  recreate (StartedAt unchanged)
- mount-consistency probe passes on a real bind mount
- persistent shell end-to-end: ``PersistentShellSession(shell_argv=...)``
  marker protocol + cwd continuation INSIDE the container
- write boundary: workspace write OK; ``/etc`` write denied (read-only
  rootfs)
- ContainerShellExecutor end-to-end: argv prefix through ``docker exec``,
  ``-w`` working dir, exit-code propagation

双 PTY 三项 (pexpect PTY ↔ docker CLI ↔ container PTY) — conclusions are
RECORDED in test comments and the sandbox-integration notepad:

① pgid/stdin-wait evidence through the relay — observed below in
   ``test_dual_pty_1_pgid_probe_through_relay``
② timeout-kill semantics — ``test_dual_pty_2_timeout_kill_orphans``
③ output-collection completeness — ``test_dual_pty_3_output_completeness``
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available"),
    pytest.mark.skipif(
        not sys.platform.startswith("linux") and sys.platform != "darwin",
        reason="same-path mount integration is POSIX-only in P1 (PRD: "
        "Windows oci persistent shells are P2; sandboxed Windows paths "
        "promise SubprocessTool only)",
    ),
]

from modex_agent.sandbox.container_executor import ContainerShellExecutor
from modex_agent.sandbox.oci_runtime import OciContainerRuntime, _container_name
from modex_agent.sandbox.selection import OciEngine
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.sandbox.types import EnforcementLevel

_IMAGE = "debian:bookworm-slim"

pexpect = pytest.importorskip("pexpect", reason="pexpect required for PTY tests")
from modex_agent.tools.terminal._persistent_session import (  # noqa: E402
    PersistentShellSession,
)


def _docker(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture(scope="module", autouse=True)
def _ensure_image() -> None:
    """Pull the test image once (docker run auto-pull could exceed the
    runtime's 120s create timeout on slow networks)."""
    probe = _docker("image", "inspect", _IMAGE)
    if probe.returncode != 0:
        pull = _docker("pull", _IMAGE, timeout=300.0)
        assert pull.returncode == 0, f"docker pull failed: {pull.stderr}"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """World-writable workspace — the container runs as uid 1000 while the
    host-side pytest dir belongs to the invoking user."""
    os.chmod(tmp_path, 0o777)
    return tmp_path


@pytest.fixture
def settings() -> SandboxSettings:
    return SandboxSettings(backend=SandboxBackend.OCI,
        network=False,
        image=_IMAGE,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE))


@pytest.fixture
def engine(workspace: Path) -> OciContainerRuntime:
    return OciContainerRuntime(engine=OciEngine.DOCKER)


@pytest.fixture(autouse=True)
def _cleanup_container(workspace: Path):
    yield
    _docker("rm", "-f", _container_name(workspace), timeout=30.0)


# ---------------------------------------------------------------------------
# Lifecycle + probe
# ---------------------------------------------------------------------------


class TestContainerLifecycleIntegration:
    async def test_probe_passes_and_enforcement_full(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        resolved = await engine.resolve(settings, workspace)
        assert resolved.backend is SandboxBackend.OCI
        assert resolved.enforcement is EnforcementLevel.FULL
        assert resolved.degraded_reason is None
        # no probe leftovers in the workspace
        assert list(workspace.glob(".modex-sbx-probe-*")) == []

    async def test_second_resolve_reuses_container(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        first = await engine.resolve(settings, workspace)
        assert first.enforcement is EnforcementLevel.FULL
        name = _container_name(workspace)
        started_1 = _docker(
            "inspect", "-f", "{{.State.StartedAt}}", name
        ).stdout.strip()

        second = await engine.resolve(settings, workspace)
        assert second.enforcement is EnforcementLevel.FULL
        started_2 = _docker(
            "inspect", "-f", "{{.State.StartedAt}}", name
        ).stdout.strip()
        # Same StartedAt → the container was neither recreated nor restarted
        assert started_1 == started_2

        # the configHash label is present on the live container
        labels = _docker(
            "inspect", "-f", "{{index .Config.Labels \"modex.sandbox.configHash\"}}", name
        )
        assert labels.returncode == 0
        assert labels.stdout.strip() != ""

    async def test_hardening_flags_on_live_container(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        await engine.resolve(settings, workspace)
        name = _container_name(workspace)
        running = _docker("inspect", "-f", "{{.State.Running}}", name)
        assert running.stdout.strip() == "true"
        ro = _docker("inspect", "-f", "{{.HostConfig.ReadonlyRootfs}}", name)
        assert ro.stdout.strip() == "true"
        net = _docker("inspect", "-f", "{{.HostConfig.NetworkMode}}", name)
        assert net.stdout.strip() == "none"
        user = _docker("inspect", "-f", "{{.Config.User}}", name)
        assert user.stdout.strip() == "1000:1000"


# ---------------------------------------------------------------------------
# Write boundary (container-side enforcement)
# ---------------------------------------------------------------------------


class TestWriteBoundaryIntegration:
    async def test_workspace_write_ok_etc_write_denied(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        resolved = await engine.resolve(settings, workspace)
        assert resolved.enforcement is EnforcementLevel.FULL
        name = _container_name(workspace)
        probe = workspace / "boundary.txt"

        ok = _docker("exec", name, "touch", str(probe))
        assert ok.returncode == 0, ok.stderr
        assert probe.exists()

        denied = _docker("exec", name, "touch", "/etc/evil")
        assert denied.returncode != 0
        assert (
            "Read-only file system" in denied.stderr
            or "read-only" in denied.stderr.lower()
        )


# ---------------------------------------------------------------------------
# ContainerShellExecutor end-to-end
# ---------------------------------------------------------------------------


class TestContainerShellExecutorIntegration:
    async def test_execute_in_container(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        resolved = await engine.resolve(settings, workspace)
        executor = ContainerShellExecutor(
            command_prefix=list(resolved.one_shot_command_argv_prefix)
        )
        out = await executor.execute("id -u", timeout=30)
        assert "1000" in out

    async def test_working_dir_and_exit_code(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        resolved = await engine.resolve(settings, workspace)
        executor = ContainerShellExecutor(
            command_prefix=list(resolved.one_shot_command_argv_prefix)
        )
        out = await executor.execute("pwd", working_dir=str(workspace), timeout=30)
        assert str(workspace) in out

        out2 = await executor.execute("ls /definitely-not-here-xyz", timeout=30)
        assert "Exit code:" in out2


# ---------------------------------------------------------------------------
# Persistent shell end-to-end (marker protocol inside the container)
# ---------------------------------------------------------------------------


class TestPersistentShellIntegration:
    async def _resolved(
        self,
        engine: OciContainerRuntime,
        settings: SandboxSettings,
        workspace: Path,
    ):
        resolved = await engine.resolve(settings, workspace)
        assert resolved.enforcement is EnforcementLevel.FULL
        return resolved

    async def test_marker_protocol_and_env_persistence(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        resolved = await self._resolved(engine, settings, workspace)
        session = PersistentShellSession(
            initial_cwd=str(workspace),
            timeout_seconds=30,
            shell_argv=list(resolved.shell_argv),
        )
        try:
            out = await session.run_command("echo hello-from-container")
            assert "hello-from-container" in out

            out = await session.run_command("export MODEX_IT=1")
            out = await session.run_command("echo $MODEX_IT")
            assert "1" in out
        finally:
            await session.close()

    async def test_cwd_continuation(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        resolved = await self._resolved(engine, settings, workspace)
        (workspace / "subdir").mkdir(exist_ok=True)
        session = PersistentShellSession(
            initial_cwd=str(workspace),
            timeout_seconds=30,
            shell_argv=list(resolved.shell_argv),
        )
        try:
            await session.run_command("cd subdir")
            out = await session.run_command("pwd")
            assert f"{workspace}/subdir" in out.replace("\r", "")
        finally:
            await session.close()

    async def test_host_container_file_identity(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        """I1 文件同一性: a shell-side write is immediately visible to host
        file tools (same bind-mounted bytes)."""
        resolved = await self._resolved(engine, settings, workspace)
        session = PersistentShellSession(
            initial_cwd=str(workspace),
            timeout_seconds=30,
            shell_argv=list(resolved.shell_argv),
        )
        try:
            await session.run_command("printf shared-bytes > identity.txt")
            host_file = workspace / "identity.txt"
            assert host_file.read_text(encoding="utf-8") == "shared-bytes"
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# 双 PTY 验证里程碑 (three hard gates)
# ---------------------------------------------------------------------------
#
# Conclusions from the WSL docker-desktop 29.1.2 run (2026-09-04) are
# recorded inline in each test AND in
# .omo/notepads/sandbox-integration/learnings.md.


class TestDualPtyMilestone:
    async def _session(
        self,
        engine: OciContainerRuntime,
        settings: SandboxSettings,
        workspace: Path,
        timeout_seconds: int = 10,
    ) -> tuple[PersistentShellSession, str]:
        resolved = await engine.resolve(settings, workspace)
        assert resolved.enforcement is EnforcementLevel.FULL
        session = PersistentShellSession(
            initial_cwd=str(workspace),
            timeout_seconds=timeout_seconds,
            shell_argv=list(resolved.shell_argv),
        )
        return session, _container_name(workspace)

    async def test_dual_pty_1_pgid_probe_through_relay(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        """① 前台进程组/输入等待证据是否经 docker CLI 中继退化.

        Mechanism: ``PersistentShellSession`` reads stdin-wait evidence via
        ``/proc`` on the *pexpect child* — the docker CLI — not the
        container bash. Two facts OBSERVED (docker-desktop 29.1.2, WSL):

        - A real container-side stdin reader (``cat``) returns the WAITING
          hint and stays answerable — ``send_input`` reaches the container
          reader through the relay (evidence flows, no degradation to the
          timeout heuristic for genuinely interactive commands).
        - A silent NON-interactive command (``sleep 999 &`` backgrounded)
          completes normally; a foreground silent ``sleep`` is classified
          prompt-kind WAITING by the echo-noise path (see milestone ②) —
          interactive detection is PARTIAL, which the PRD accepts as the
          degraded-but-not-broken outcome.
        """
        session, name = await self._session(engine, settings, workspace)
        try:
            # backgrounding survives the relay: wrapper completes despite &
            out = await session.run_command("sleep 999 & echo bg-started")
            assert "bg-started" in out

            # Linux has /proc evidence through the relay; macOS does not.
            out = await session.run_command("cat")
            if sys.platform.startswith("linux"):
                assert "[hint:" in out, (
                    f"stdin-wait evidence degraded past the hint layer: {out[:200]!r}"
                )
                answered = await session.send_input("probe-line")
                assert "probe-line" in answered
            else:
                assert "timed out" in out
        finally:
            await session.close()
            _docker(
                "exec", name, "sh", "-c",
                'for f in /proc/[0-9]*/comm; do p=${f#/proc/}; p=${p%/comm}; '
                '[ "$p" = 1 ] && continue; read c < "$f" 2>/dev/null; '
                '[ "$c" = sleep ] && kill "$p" 2>/dev/null; done; exit 0',
                timeout=30.0,
            )

    async def test_dual_pty_2_timeout_kill_orphans(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        """② timeout kill 语义: killing the CLI — do container-side
        processes leak?

        OBSERVED (docker-desktop 29.1.2, WSL): a silent ``sleep 999``
        returns the prompt-kind WAITING hint (NOT the timeout message) —
        the relay ECHOES the wrapper line, whose trailing shape matches
        the layer-2 prompt-suffix detector, before the 8s deadline. The
        session stays answerable: ``send_input(^C)`` interrupts and the
        shell returns to IDLE. Killing the CLI (close/timeout path) tears
        the container-side process down too — ``pgrep sleep`` finds
        NOTHING after the kill. No orphan cleanup is required on this
        engine. Acceptance: interactive detection here is PARTIAL (echo
        noise can classify silent commands as prompt-kind waits), but
        never a hang and never a leak — the milestone gate.
        """
        session, name = await self._session(engine, settings, workspace)
        try:
            out = await session.run_command("sleep 999")
            verdict = (
                "waiting-hint" if "[hint:" in out
                else "timeout" if "timed out" in out
                else "completed"
            )
            assert verdict in {"waiting-hint", "timeout"}, (
                f"sleep 999 completed unexpectedly: {out[:200]!r}"
            )
            if verdict == "waiting-hint":
                # the session stays answerable through the relay
                await session.send_input("^C")
        finally:
            await session.close()

        await asyncio.sleep(1.0)  # daemon teardown grace
        # Orphan check WITHOUT procps (neither debian:bookworm-slim nor
        # modex-sandbox ships ps/pgrep): scan /proc/<pid>/comm via the shell,
        # skipping PID 1 (the container's own `sleep infinity`).
        leaked = _docker(
            "exec", name, "sh", "-c",
            "for f in /proc/[0-9]*/comm; do p=${f#/proc/}; p=${p%/comm}; "
            '[ "$p" = 1 ] && continue; read c < "$f" 2>/dev/null; '
            '[ "$c" = sleep ] && echo "$f"; done; exit 0',
        )
        assert leaked.returncode == 0, leaked.stderr
        if sys.platform.startswith("linux"):
            assert leaked.stdout.strip() == "", (
                f"container-side orphans survived the CLI kill: {leaked.stdout!r} — "
                "milestone finding; session-reset needs orphan cleanup"
            )
        elif leaked.stdout.strip():
            cleanup = _docker(
                "exec",
                name,
                "sh",
                "-c",
                "for f in /proc/[0-9]*/comm; do p=${f#/proc/}; p=${p%/comm}; "
                '[ "$p" = 1 ] && continue; read c < "$f" 2>/dev/null; '
                '[ "$c" = sleep ] && kill "$p" 2>/dev/null; done; exit 0',
            )
            assert cleanup.returncode == 0, cleanup.stderr

    async def test_dual_pty_3_output_completeness(
        self, engine: OciContainerRuntime, settings: SandboxSettings, workspace: Path
    ) -> None:
        """③ 输出采集完整性: long output and interactive echo survive the
        pexpect PTY ↔ docker CLI ↔ container PTY chain.

        OBSERVED (docker-desktop 29.1.2, WSL): 2000-line seq output arrives
        complete (head and tail intact); send_input reaches the
        container-side reader through the relay.
        """
        session, _ = await self._session(engine, settings, workspace)
        try:
            out = await session.run_command("seq 1 2000")
            assert "1" in out and "1999" in out and "2000" in out
            assert "1998" in out  # interior survived too

            # Interactive evidence is available through /proc on Linux only.
            out = await session.run_command("read v; echo got-$v")
            if sys.platform.startswith("linux"):
                assert "[hint:" in out
                out = await session.send_input("relay-ok")
                assert "got-relay-ok" in out
            else:
                assert "timed out" in out
        finally:
            await session.close()
