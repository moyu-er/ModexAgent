"""Execution-owner regressions: unavailable is not denied or already executed."""

from __future__ import annotations

import errno
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.sandbox.exceptions import SandboxUnavailableError
from modex_agent.sandbox.runtime import ResolvedSandbox, SandboxRuntime
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.sandbox.shell_plan import ShellAssemblyDeps, build_bash_tool
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.tools.terminal.persistent_bash import (
    BashInputTool,
    PersistentBashTool,
    ensure_input_companion,
)
from modex_agent.tools.terminal.subprocess_tool import SubprocessTool
from modex_agent.tools.workspace_scoped import WorkspaceRootProvider


class FixedRoot(WorkspaceRootProvider):
    def __init__(self, root: Path) -> None:
        self.root = root

    def current(self) -> Path:
        return self.root


class FailedRuntime(SandboxRuntime):
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def resolve(self, settings: SandboxSettings, workspace_root: Path) -> ResolvedSandbox:
        raise self.error


@pytest.mark.parametrize("error", [SandboxUnavailableError("engine offline"), FileNotFoundError("engine missing"), OSError(errno.ENOSPC, "probe storage full")])
async def test_initialization_unavailable_keeps_host_bash(tmp_path: Path, error: Exception) -> None:
    from modex_agent.sandbox.decision import SecurityDecisionService
    from modex_agent.sandbox.interceptor import SandboxGuardInterceptor

    settings = SandboxSettings(backend=SandboxBackend.LOCAL)
    root = FixedRoot(tmp_path)
    guard = SandboxGuardInterceptor(settings, FailedRuntime(error), root, SecurityDecisionService(settings, root))
    resolved = await guard.ensure_resolved()
    assert resolved.backend is SandboxBackend.HOST
    assert str(error) in (resolved.degraded_reason or "")
    assert build_bash_tool(ShellAssemblyDeps(resolved=resolved, pty_supported=False)).name == "bash"


@pytest.mark.parametrize("error", [PermissionError("denied"), ValueError("bad config"), TypeError("bug"), OSError(errno.EINVAL, "bad argument")])
async def test_initialization_errors_are_not_unavailability(tmp_path: Path, error: Exception) -> None:
    from modex_agent.sandbox.decision import SecurityDecisionService
    from modex_agent.sandbox.interceptor import SandboxGuardInterceptor

    settings = SandboxSettings(backend=SandboxBackend.LOCAL)
    root = FixedRoot(tmp_path)
    guard = SandboxGuardInterceptor(settings, FailedRuntime(error), root, SecurityDecisionService(settings, root))
    with pytest.raises(type(error), match=str(error).replace("[", r"\[").replace("]", r"\]")):
        await guard.ensure_resolved()


def local_substrate() -> ResolvedSandbox:
    return ResolvedSandbox(backend=SandboxBackend.LOCAL, enforcement=EnforcementLevel.FULL,
                           shell_argv=["env", "/bin/bash", "--noprofile", "--norc", "-i"],
                           one_shot_command_argv_prefix=["env"])


def test_persistent_manager_receives_cwd_and_output_budget(tmp_path: Path) -> None:
    tool = build_bash_tool(ShellAssemblyDeps(resolved=local_substrate(), root_provider=FixedRoot(tmp_path)))
    assert isinstance(tool, PersistentBashTool)
    assert tool.manager._initial_cwd == str(tmp_path)
    assert tool.manager.max_output_chars is None


def test_shell_assembly_uses_the_canonical_mount_cwd(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    tool = build_bash_tool(ShellAssemblyDeps(resolved=local_substrate(), root_provider=FixedRoot(tmp_path / "sub" / "..")))
    assert isinstance(tool, PersistentBashTool)
    assert tool.manager._initial_cwd == str(tmp_path)


@pytest.mark.parametrize("sandboxed", [False, True])
async def test_one_shot_uses_workspace_default_and_explicit_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sandboxed: bool) -> None:
    resolved = local_substrate().model_copy(update={"backend": SandboxBackend.OCI, "one_shot_command_argv_prefix": ["docker", "exec", "fixture"]}) if sandboxed else None
    tool = build_bash_tool(ShellAssemblyDeps(resolved=resolved, pty_supported=False, root_provider=FixedRoot(tmp_path)))
    assert isinstance(tool, SubprocessTool)
    execute = AsyncMock(return_value="ok")
    monkeypatch.setattr(tool._executor, "execute", execute)
    await tool.execute(command="pwd")
    assert execute.call_args.args[1] == str(tmp_path)
    await tool.execute(command="pwd", working_dir=str(tmp_path / "sub"))
    assert execute.call_args.args[1] == str(tmp_path / "sub")
    await tool.execute(command="pwd", working_dir="sub")
    assert execute.call_args.args[1] == str(tmp_path / "sub")


def test_one_shot_removes_only_stale_persistent_companion() -> None:
    from modex_agent.tools.manager import InMemoryToolManager
    manager = InMemoryToolManager()
    old = PersistentBashTool()
    manager.register(BashInputTool(old.manager))
    tool = build_bash_tool(ShellAssemblyDeps(pty_supported=False))
    ensure_input_companion(manager, tool)
    assert manager.get_tool("bash_input") is None


async def test_local_one_shot_keeps_prefix_and_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    process = AsyncMock()
    process.communicate.return_value = (b"ok", b"")
    process.returncode = 0
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("MODEX_HOST_FIDELITY", "inherited")
    tool = build_bash_tool(ShellAssemblyDeps(resolved=local_substrate(), pty_supported=False, root_provider=FixedRoot(tmp_path)))
    await tool.execute(command="printf '%s' 'a b' | cat")
    assert spawn.call_args.args == ("env", "/bin/bash", "--noprofile", "--norc", "-c", "printf '%s' 'a b' | cat")
    assert spawn.call_args.kwargs["cwd"] == str(tmp_path)
    assert spawn.call_args.kwargs["env"]["MODEX_HOST_FIDELITY"] == "inherited"


async def test_oci_one_shot_translates_windows_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from modex_agent.sandbox.container_executor import ContainerShellExecutor
    process = AsyncMock()
    process.communicate.return_value = (b"ok", b"")
    process.returncode = 0
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await ContainerShellExecutor(["podman", "exec", "fixture"]).execute("pwd", r"F:\work space\sub")
    assert spawn.call_args.args[:5] == ("podman", "exec", "-w", "/f/work space/sub", "fixture")


async def test_actual_local_launch_unavailable_after_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modex_agent.sandbox.bwrap_runtime import BwrapRuntime
    from modex_agent.sandbox.platform import Platform
    monkeypatch.setattr("modex_agent.sandbox.bwrap_runtime._get_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("modex_agent.sandbox.bwrap_runtime._resolve_host_shell", lambda: "/bin/bash")
    calls = []
    def cannot_launch(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "bwrap: Creating new namespace failed: Operation not permitted")
    monkeypatch.setattr(subprocess, "run", cannot_launch)
    resolved = await BwrapRuntime().resolve_available(SandboxSettings(backend=SandboxBackend.LOCAL, exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE)), tmp_path)
    assert resolved.backend is SandboxBackend.HOST
    assert "namespace" in (resolved.degraded_reason or "")
    assert calls[0][-5:] == ["/bin/bash", "--noprofile", "--norc", "-c", ":"]


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
async def test_failed_shell_start_does_not_send_target(tmp_path: Path) -> None:
    from modex_agent.tools.terminal._persistent_session import PersistentShellSession
    from modex_agent.tools.terminal.persistent_bash import PersistentShellStartError
    session = PersistentShellSession(initial_cwd=str(tmp_path), shell_argv=["/bin/sh", "-c", "printf startup-failed >&2; exit 1"])
    try:
        with pytest.raises(PersistentShellStartError, match="not sent.*startup-failed"):
            await session.run_command("touch must-not-exist")
        assert not (tmp_path / "must-not-exist").exists()
    finally:
        await session.close()


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
async def test_side_effect_then_shell_exit_is_uncertain_and_not_replayed(tmp_path: Path) -> None:
    from modex_agent.tools.terminal._persistent_session import PersistentShellSession
    session = PersistentShellSession(initial_cwd=str(tmp_path))
    try:
        result = await session.run_command("printf once >> effect; exit")
        assert "uncertain" in result
        assert (tmp_path / "effect").read_text() == "once"
        assert "next" in await session.run_command("printf next")
        assert (tmp_path / "effect").read_text() == "once"
    finally:
        await session.close()


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
async def test_shell_pair_real_cwd_env_pipeline_and_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modex_agent.tools.manager import InMemoryToolManager
    monkeypatch.setenv("MODEX_HOST_FIDELITY", "inherited")
    tool = build_bash_tool(ShellAssemblyDeps(resolved=local_substrate(), root_provider=FixedRoot(tmp_path)))
    assert isinstance(tool, PersistentBashTool)
    manager = InMemoryToolManager()
    ensure_input_companion(manager, tool)
    companion = manager.get_tool("bash_input")
    assert isinstance(companion, BashInputTool)
    try:
        assert str(tmp_path) in await tool.execute("pwd")
        await tool.execute("mkdir sub; cd sub; printf '%s' \"$MODEX_HOST_FIDELITY\" | cat > value")
        assert (tmp_path / "sub" / "value").read_text() == "inherited"
        assert str(tmp_path / "sub") in await tool.execute("pwd")
        assert "[hint:" in await tool.execute("read -p 'Name: ' answer; printf 'got:%s' \"$answer\"")
        assert "got:paired" in await companion.execute("paired")
    finally:
        await tool.close()


async def test_seatbelt_cleanup_only_reclaims_its_own_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modex_agent.sandbox.platform import Platform
    from modex_agent.sandbox.seatbelt_runtime import SeatbeltRuntime
    monkeypatch.setattr("modex_agent.sandbox.seatbelt_runtime._get_platform", lambda: Platform.MACOS)
    first, second = SeatbeltRuntime(), SeatbeltRuntime()
    settings = SandboxSettings(backend=SandboxBackend.LOCAL)
    a = await first.resolve(settings, tmp_path)
    b = await second.resolve(settings, tmp_path)
    own, shared = Path(a.one_shot_command_argv_prefix[2]), Path(b.one_shot_command_argv_prefix[2])
    try:
        await first.close()
        assert not own.exists()
        assert shared.exists()
        await first.close()
    finally:
        own.unlink(missing_ok=True)
        shared.unlink(missing_ok=True)


async def test_oci_dead_result_reports_uncertainty_without_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from modex_agent.sandbox.container_executor import ContainerShellExecutor
    process = AsyncMock()
    process.communicate.return_value = (b"side effect happened\n", b"Error: container is not running")
    process.returncode = 1
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    result = await ContainerShellExecutor(["docker", "exec", "fixture"]).execute("do-work")
    assert "uncertain" in result
    assert "side effect happened" in result
    assert spawn.await_count == 1


async def test_operation_permission_denied_keeps_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from modex_agent.sandbox.container_executor import ContainerShellExecutor
    process = AsyncMock()
    process.communicate.return_value = (b"", b"Operation not permitted")
    process.returncode = 1
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    executor = ContainerShellExecutor(["docker", "exec", "fixture"])
    result = await executor.execute("do-work")
    assert "Operation not permitted" in result
    assert spawn.await_count == 1
    assert spawn.call_args.args[:3] == ("docker", "exec", "fixture")


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
async def test_transport_error_after_send_does_not_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modex_agent.tools.terminal._persistent_session import PersistentShellSession
    session = PersistentShellSession(initial_cwd=str(tmp_path))
    collect = session._collect
    async def disconnect(pending):
        await collect(pending)
        raise ConnectionResetError("relay disconnected")
    monkeypatch.setattr(session, "_collect", disconnect)
    try:
        result = await session.run_command("printf once >> effect")
        assert "uncertain" in result
        assert "relay disconnected" in result
        assert (tmp_path / "effect").read_text() == "once"
    finally:
        await session.close()


@pytest.mark.parametrize("engine", ["bwrap", "seatbelt", "oci"])
async def test_runtime_compiles_canonical_relative_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str,
) -> None:
    from modex_agent.sandbox.bwrap_runtime import BwrapRuntime
    from modex_agent.sandbox.oci_runtime import OciContainerRuntime
    from modex_agent.sandbox.oci_support import CliResult, ContainerMount
    from modex_agent.sandbox.platform import Platform
    from modex_agent.sandbox.seatbelt_runtime import SeatbeltRuntime
    from modex_agent.workspace.boundary import canonicalize_path

    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    (shared / ".git").mkdir()
    (tmp_path / "protected").mkdir()
    settings = SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE, writable_roots=[Path("../shared")], protected_subpaths=[".git", "../protected"]))
    monkeypatch.setattr("modex_agent.sandbox.bwrap_runtime._get_platform", lambda: Platform.LINUX)
    monkeypatch.setattr("modex_agent.sandbox.seatbelt_runtime._get_platform", lambda: Platform.MACOS)
    monkeypatch.setattr(SandboxRuntime, "_validate_startup", AsyncMock())
    monkeypatch.setattr(BwrapRuntime, "_validate_startup", AsyncMock())
    monkeypatch.setattr(SeatbeltRuntime, "_validate_startup", AsyncMock())
    async def cli(argv, **kwargs):
        return CliResult(returncode=1, stderr="no such object") if argv[1] == "inspect" else CliResult(returncode=0)
    monkeypatch.setattr("modex_agent.sandbox.oci_runtime._run_cli", cli)
    runtime = {"bwrap": BwrapRuntime, "seatbelt": SeatbeltRuntime, "oci": OciContainerRuntime}[engine]()
    try:
        resolved = await runtime.resolve_available(settings, workspace / ".." / "workspace")
        expected = canonicalize_path("../shared", base=canonicalize_path(workspace))
        if engine == "bwrap":
            prefix = resolved.one_shot_command_argv_prefix
            assert ["--bind", str(expected), str(expected)] == prefix[prefix.index(str(expected)) - 1:prefix.index(str(expected)) + 2]
            assert str(expected / ".git") in prefix
            assert str(tmp_path / "protected") in prefix
        elif engine == "seatbelt":
            profile = Path(resolved.one_shot_command_argv_prefix[2]).read_text()
            assert f'(allow file-write* (subpath "{expected.as_posix()}"))' in profile
            assert f'(deny file-write* (subpath "{(expected / ".git").as_posix()}"))' in profile
            assert f'(deny file-write* (subpath "{(tmp_path / "protected").as_posix()}"))' in profile
        else:
            assert resolved.mount_table is not None
            assert any(m.host_path == expected and m.sandbox_path == Path(ContainerMount.for_path(expected).sandbox_path) for m in resolved.mount_table)
            assert any(m.host_path == expected / ".git" and m.read_only for m in resolved.mount_table)
            assert any(m.host_path == tmp_path / "protected" and m.read_only for m in resolved.mount_table)
    finally:
        await runtime.close()


# Real-bwrap enforcement: skip wherever the binary is absent (CI runners,
# Windows, macOS) — the same availability contract test_bwrap_integration
# skips under; the parameter-compilation contracts are covered engine-free
# above.
_HAS_REAL_BWRAP = sys.platform.startswith("linux") and shutil.which("bwrap") is not None


@pytest.mark.skipif(not _HAS_REAL_BWRAP, reason="requires real Linux bwrap")
async def test_real_bwrap_writes_permitted_relative_extra_root(tmp_path: Path) -> None:
    from modex_agent.sandbox.bwrap_runtime import BwrapRuntime
    from modex_agent.sandbox.container_executor import ContainerShellExecutor
    from modex_agent.sandbox.decision import SecurityDecisionService
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    workspace.mkdir()
    shared.mkdir()
    settings = SandboxSettings(backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=WriteSurface.WORKSPACE, writable_roots=[Path("../shared")]))
    assert SecurityDecisionService(settings, FixedRoot(workspace)).evaluate_file_tool("write", "../shared/value").is_clean
    resolved = await BwrapRuntime().resolve_available(settings, workspace)
    assert resolved.backend is SandboxBackend.LOCAL, resolved.degraded_reason
    executor = ContainerShellExecutor(resolved.one_shot_command_argv_prefix, backend=SandboxBackend.LOCAL)
    output = await executor.execute("printf permitted > ../shared/value", working_dir=str(workspace))
    assert (shared / "value").is_file(), output
    assert (shared / "value").read_text() == "permitted"


async def assembled_shell(tmp_path: Path, resolved: ResolvedSandbox, pty: bool, monkeypatch: pytest.MonkeyPatch):
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
    from modex_agent.plugins.defaults.tools import BashToolFactory, ToolConfig
    from modex_agent.sandbox.decision import SecurityDecisionService
    from modex_agent.sandbox.interceptor import SandboxGuardInterceptor
    class FixedRuntime(SandboxRuntime):
        async def resolve(self, settings, workspace_root):
            return resolved
    root = FixedRoot(tmp_path)
    settings = SandboxSettings(backend=resolved.backend)
    guard = SandboxGuardInterceptor(settings, FixedRuntime(), root, SecurityDecisionService(settings, root))
    ctx = AgentContext(registry=MagicMock(), workspace_ctx=MagicMock(), agent_name="fixture",
                       pool_runtime=PoolRuntimeDeps(root_provider=root, interceptor_chain=InterceptorChain([guard])))
    monkeypatch.setattr("modex_agent.plugins.defaults.tools.persistent_bash_supported", lambda: pty)
    tool = await BashToolFactory().create(ToolConfig(), ctx)
    return guard, tool


async def invoke_shell(guard, tool, command, state):
    from modex_agent.core.message import ToolCall
    from modex_agent.core.tool_manager import ToolResult
    from modex_agent.interceptor.abc import ToolCallContext
    call = ToolCallContext(tool_call=ToolCall(tool_name="bash", arguments={"command": command}, call_id="call"),
                           tool_name="bash", arguments={"command": command}, session_id="fixture", turn_id="turn")
    async def execute():
        return ToolResult.from_text("bash", await tool.execute(command=command))
    return await guard.around_tool_call(SimpleNamespace(runtime=SimpleNamespace(state=state)), call, execute)


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
async def test_bound_start_failure_runs_host_once_and_keeps_companion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.tools.manager import InMemoryToolManager
    argv = [str(tmp_path / "missing-engine")]
    resolved = local_substrate().model_copy(update={"shell_argv": argv})
    guard, tool = await assembled_shell(tmp_path, resolved, True, monkeypatch)
    manager = InMemoryToolManager()
    ensure_input_companion(manager, tool)
    companion = manager.get_tool("bash_input")
    assert isinstance(tool, PersistentBashTool)
    assert isinstance(companion, BashInputTool)
    state = ReActTurnState()
    monkeypatch.setenv("MODEX_BINDING_ENV", "inherited")
    try:
        result = await invoke_shell(guard, tool, 'printf "$MODEX_BINDING_ENV" >> effect', state)
        assert result.error is None
        assert (tmp_path / "effect").read_text() == "inherited"
        snapshot = state.custom[TurnCustomKey.SANDBOX_ENFORCEMENT]
        assert snapshot.backend is SandboxBackend.HOST
        assert snapshot.enforcement is EnforcementLevel.NONE
        assert snapshot.degraded_reason
        assert (await guard.ensure_resolved()).backend is SandboxBackend.HOST
        assert companion.manager is tool.manager
        assert "[hint:" in await tool.execute("read -p 'Name: ' answer; printf 'got:%s' \"$answer\"")
        assert "got:paired" in await companion.execute("paired")
        assert (tmp_path / "effect").read_text() == "inherited"
    finally:
        await tool.close()


@pytest.mark.parametrize("backend", [SandboxBackend.LOCAL, SandboxBackend.OCI])
async def test_bound_missing_cli_runs_host_once_and_reports_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: SandboxBackend) -> None:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.runtime.enums import TurnCustomKey
    prefix = [str(tmp_path / "missing-engine")]
    if backend is SandboxBackend.OCI:
        prefix.extend(["exec", "fixture"])
    resolved = local_substrate().model_copy(update={"backend": backend,
        "one_shot_command_argv_prefix": prefix})
    guard, tool = await assembled_shell(tmp_path, resolved, False, monkeypatch)
    state = ReActTurnState()
    result = await invoke_shell(guard, tool, "printf once >> effect", state)
    assert result.error is None
    assert (tmp_path / "effect").read_text() == "once"
    assert state.custom[TurnCustomKey.SANDBOX_ENFORCEMENT].backend is SandboxBackend.HOST
    assert (await guard.ensure_resolved()).backend is SandboxBackend.HOST
    await invoke_shell(guard, tool, "printf next", state)
    assert (tmp_path / "effect").read_text() == "once"


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
@pytest.mark.parametrize("tail", ["exit", "printf 'Operation not permitted' >&2; false"])
async def test_bound_target_failure_never_changes_substrate_or_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tail: str) -> None:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.runtime.enums import TurnCustomKey
    guard, tool = await assembled_shell(tmp_path, local_substrate(), True, monkeypatch)
    assert isinstance(tool, PersistentBashTool)
    state = ReActTurnState()
    try:
        await invoke_shell(guard, tool, f"printf once >> effect; {tail}", state)
        assert (tmp_path / "effect").read_text() == "once"
        assert (await guard.ensure_resolved()).backend is SandboxBackend.LOCAL
        assert state.custom[TurnCustomKey.SANDBOX_ENFORCEMENT].backend is SandboxBackend.LOCAL
        await invoke_shell(guard, tool, "printf next", state)
        assert (tmp_path / "effect").read_text() == "once"
    finally:
        await tool.close()


async def test_bound_nonzero_cli_exit_never_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.runtime.enums import TurnCustomKey
    script = f"from pathlib import Path; import sys; Path({str(tmp_path / 'effect')!r}).write_text('once'); sys.stderr.write('container is not running'); sys.exit(125)"
    engine_script = tmp_path / "fake_engine.py"
    engine_script.write_text(script)
    resolved = local_substrate().model_copy(update={"backend": SandboxBackend.OCI,
        "one_shot_command_argv_prefix": [sys.executable, str(engine_script), "fixture"]})
    guard, tool = await assembled_shell(tmp_path, resolved, False, monkeypatch)
    state = ReActTurnState()
    await invoke_shell(guard, tool, "printf replayed >> effect", state)
    assert (tmp_path / "effect").read_text() == "once"
    assert state.custom[TurnCustomKey.SANDBOX_ENFORCEMENT].backend is SandboxBackend.OCI
    assert (await guard.ensure_resolved()).backend is SandboxBackend.OCI


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
async def test_fallback_does_not_replace_another_conversations_live_shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from modex_agent.agents.react.state import ReActTurnState
    from modex_agent.runtime.enums import TurnCustomKey
    from modex_agent.runtime.env_context import _current_session_id
    launcher = tmp_path / "launcher"
    launcher.write_text("#!/bin/sh\nexec /bin/bash --noprofile --norc -i\n")
    launcher.chmod(0o700)
    argv = [str(launcher)]
    guard, tool = await assembled_shell(tmp_path, local_substrate().model_copy(update={"shell_argv": argv}), True, monkeypatch)
    assert isinstance(tool, PersistentBashTool)
    token = _current_session_id.set("live")
    try:
        await tool.execute("export KEEP_SESSION=retained")
        launcher.unlink()
        _current_session_id.set("failed")
        state = ReActTurnState()
        await invoke_shell(guard, tool, "printf once >> effect", state)
        assert state.custom[TurnCustomKey.SANDBOX_ENFORCEMENT].backend is SandboxBackend.HOST
        _current_session_id.set("live")
        assert (await guard.ensure_resolved()).backend is SandboxBackend.LOCAL
        assert await tool.execute('printf "$KEEP_SESSION"') == "retained"
        assert (tmp_path / "effect").read_text() == "once"
    finally:
        await tool.close()
        _current_session_id.reset(token)


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
async def test_invalid_spawn_configuration_is_not_unavailability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import pexpect
    guard, tool = await assembled_shell(tmp_path, local_substrate(), True, monkeypatch)
    assert isinstance(tool, PersistentBashTool)
    def invalid(*args, **kwargs):
        raise OSError(errno.EINVAL, "invalid spawn configuration")
    monkeypatch.setattr(pexpect, "spawn", invalid)
    try:
        with pytest.raises(OSError, match="invalid spawn configuration"):
            await tool.execute("printf should-not-run")
        assert (await guard.ensure_resolved()).backend is SandboxBackend.LOCAL
    finally:
        await tool.close()


@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
async def test_bound_one_shot_permission_error_never_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    import asyncio
    guard, tool = await assembled_shell(tmp_path, local_substrate(), False, monkeypatch)
    binding = await guard.execution_binding()
    downgrade = AsyncMock(wraps=binding.fallback)
    monkeypatch.setattr(binding, "fallback", downgrade)
    denied = PermissionError(code, "launcher denied")
    spawn = AsyncMock(side_effect=denied)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(PermissionError) as caught:
        await tool.execute(command="printf forbidden >> effect")
    assert caught.value is denied
    assert (await guard.ensure_resolved()).backend is SandboxBackend.LOCAL
    assert spawn.await_count == 1
    downgrade.assert_not_awaited()
    assert not (tmp_path / "effect").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
@pytest.mark.parametrize("code", [errno.EACCES, errno.EPERM])
async def test_bound_persistent_permission_error_never_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    import pexpect
    guard, tool = await assembled_shell(tmp_path, local_substrate(), True, monkeypatch)
    assert isinstance(tool, PersistentBashTool)
    binding = await guard.execution_binding()
    downgrade = AsyncMock(wraps=binding.fallback)
    monkeypatch.setattr(binding, "fallback", downgrade)
    denied = PermissionError(code, "launcher denied")
    spawn = MagicMock(side_effect=denied)
    monkeypatch.setattr(pexpect, "spawn", spawn)
    try:
        with pytest.raises(PermissionError) as caught:
            await tool.execute("printf forbidden >> effect")
        assert caught.value is denied
        assert (await guard.ensure_resolved()).backend is SandboxBackend.LOCAL
        assert spawn.call_count == 1
        downgrade.assert_not_awaited()
        assert not (tmp_path / "effect").exists()
    finally:
        await tool.close()


@pytest.mark.skipif(sys.platform == "win32", reason="requires POSIX PTY")
@pytest.mark.parametrize("message", ["Permission denied", "Operation not permitted", "invalid launcher config", ""])
async def test_bound_early_launcher_exit_never_downgrades(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    from modex_agent.tools.terminal.persistent_bash import PersistentShellStartError
    argv = ["/bin/sh", "-c", f"printf '%s' '{message}' >&2; exit 1"]
    guard, tool = await assembled_shell(tmp_path, local_substrate().model_copy(update={"shell_argv": argv}), True, monkeypatch)
    assert isinstance(tool, PersistentBashTool)
    binding = await guard.execution_binding()
    downgrade = AsyncMock(wraps=binding.fallback)
    monkeypatch.setattr(binding, "fallback", downgrade)
    try:
        with pytest.raises(PersistentShellStartError, match="not sent"):
            await tool.execute("printf forbidden >> effect")
        assert (await guard.ensure_resolved()).backend is SandboxBackend.LOCAL
        downgrade.assert_not_awaited()
        assert not (tmp_path / "effect").exists()
    finally:
        await tool.close()


@pytest.mark.parametrize("failure_kind", ["error", "interrupt", "cancel"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_factory_reclaims_only_owned_profiles_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str, cleanup_fails: bool,
) -> None:
    import asyncio

    from modex_agent.plugins.assembly.context import AgentContext, PoolRuntimeDeps
    from modex_agent.plugins.defaults.interceptors import (
        SandboxGuardConfig,
        SandboxGuardInterceptorFactory,
    )
    from modex_agent.sandbox.platform import Platform
    from modex_agent.sandbox.seatbelt_runtime import SeatbeltRuntime
    from modex_graph.exceptions import GraphInterrupt

    monkeypatch.setattr("modex_agent.sandbox.seatbelt_runtime.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("modex_agent.sandbox.seatbelt_runtime._get_platform", lambda: Platform.MACOS)
    runtime, shared = SeatbeltRuntime(), SeatbeltRuntime()
    settings = SandboxSettings(backend=SandboxBackend.LOCAL)
    shared_resolved = await shared.resolve(settings, tmp_path)
    shared_profile = Path(shared_resolved.one_shot_command_argv_prefix[2])
    assert len(list(tmp_path.glob("modex-seatbelt-*.sb"))) == 1
    entered = asyncio.Event()
    failure = GraphInterrupt("approval") if failure_kind == "interrupt" else ValueError("init failed")
    async def fail_after_profile(resolved, workspace_root):
        assert len(list(tmp_path.glob("modex-seatbelt-*.sb"))) == 2
        entered.set()
        if failure_kind == "cancel":
            await asyncio.Event().wait()
        raise failure
    monkeypatch.setattr(runtime, "_validate_startup", fail_after_profile)
    original_close = runtime.close
    async def close():
        await original_close()
        if cleanup_fails:
            raise RuntimeError("cleanup failed after releasing profile")
    monkeypatch.setattr(runtime, "close", close)
    monkeypatch.setattr("modex_agent.plugins.defaults.interceptors.resolve_selection", AsyncMock())
    monkeypatch.setattr("modex_agent.plugins.defaults.interceptors.select_runtime", lambda selection: runtime)
    ctx = AgentContext(registry=MagicMock(), workspace_ctx=MagicMock(), agent_name="fixture",
                       pool_runtime=PoolRuntimeDeps(root_provider=FixedRoot(tmp_path)))
    task = asyncio.create_task(SandboxGuardInterceptorFactory().create(SandboxGuardConfig(sandbox=settings), ctx))
    try:
        await entered.wait()
        if failure_kind == "cancel":
            task.cancel("cancel init")
            with pytest.raises(asyncio.CancelledError, match="cancel init"):
                await task
        else:
            with pytest.raises(type(failure)) as caught:
                await task
            assert caught.value is failure
        assert len(list(tmp_path.glob("modex-seatbelt-*.sb"))) == 1
        assert shared_profile.exists()
    finally:
        await original_close()
        await shared.close()
