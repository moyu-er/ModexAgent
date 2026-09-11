"""Real macOS kernel enforcement, without mocking the probe or launchers.

These tests call the shell tools directly: no command-text guard can substitute
for Seatbelt denying the descendant process's actual filesystem operations.
"""

from __future__ import annotations

import shlex
import shutil
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.core.llm_struct import RuntimeSafetyPolicy
from modex_agent.core.provider import LLMProvider
from modex_agent.plugins.assembly.single_agent import (
    SingleAgentAssembled,
    SingleAgentInfra,
    assemble_declared_single_agent,
)
from modex_agent.plugins.defaults import DefaultPlugin
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.sandbox.selection import resolve_selection, select_runtime
from modex_agent.sandbox.settings import (
    ExclusiveConfig,
    SandboxBackend,
    SandboxSettings,
    WriteSurface,
)
from modex_agent.sandbox.shell_plan import resolved_binding
from modex_agent.sandbox.types import EnforcementLevel
from modex_agent.scope.compiler import compile_scope
from modex_agent.scope.spec import AgentSpec, PoolSpec, ScopeKind, ScopeSpec
from modex_agent.tools.presets import ToolPreset
from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.paths import WorkspacePaths

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="requires the real macOS Seatbelt executable",
)


async def test_real_selection_and_compiled_profile_start_bash(tmp_path: Path) -> None:
    selection = await resolve_selection(SandboxBackend.LOCAL)
    assert selection.effective is SandboxBackend.LOCAL, selection.degraded_reason
    runtime = select_runtime(selection)
    try:
        resolved = await runtime.resolve_available(
            SandboxSettings(backend=SandboxBackend.LOCAL), tmp_path
        )
        assert resolved.backend is SandboxBackend.LOCAL, resolved.degraded_reason
        assert resolved.enforcement is EnforcementLevel.FULL
        assert resolved.degraded_reason is None
    finally:
        await runtime.close()


@asynccontextmanager
async def _agent(
    tmp_path: Path, settings: SandboxSettings, mode: str
) -> AsyncIterator[SingleAgentAssembled]:
    workspace = tmp_path / 'workspace "quoted"'
    workspace.mkdir()
    (workspace / "agents").mkdir()
    (workspace / "agents" / "main.md").write_text("Seatbelt test agent.")
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        DefaultPlugin().register(registration)
    declaration = ScopeSpec(
        kind=ScopeKind.POOL,
        pool=PoolSpec(
            name="seatbelt",
            agents=[AgentSpec(
                name="main",
                toolset=ToolPreset.NONE,
                capabilities={"shell": {"mode": mode}, "skills": False},
                interceptors=["sandbox_guard"],
                interceptor_configs={
                    "sandbox_guard": {"sandbox": settings.model_dump(mode="json")},
                },
            )],
        ),
    )
    compiled = compile_scope(
        declaration,
        workspace_ctx=WorkspaceContext(
            target=workspace, paths=WorkspacePaths(root=tmp_path / "data"), is_home=False,
        ),
        registry=registry,
    ).agents[0]
    assembled = await assemble_declared_single_agent(
        compiled,
        SingleAgentInfra(
            llm_provider=MagicMock(spec=LLMProvider),
            safety=RuntimeSafetyPolicy(),
            root_provider=None,
        ),
        project_dir=workspace,
        data_dir=tmp_path / "data",
        component_registry=registry,
    )
    try:
        assert assembled.instance.pipeline is not None
        binding = await resolved_binding(assembled.instance.pipeline.interceptor_chain)
        assert binding is not None
        assert binding.current().backend is SandboxBackend.LOCAL
        assert binding.current().degraded_reason is None
        profile = Path(binding.current().one_shot_command_argv_prefix[2])
        assert profile.is_file()
        yield assembled
        # An operation denial must never authorize a replay on HOST.
        assert binding.current().backend is SandboxBackend.LOCAL
        assert binding.current().degraded_reason is None
    finally:
        await assembled.close()
    assert not profile.exists()


def _python_command(code: str) -> str:
    return shlex.join([sys.executable, "-B", "-c", code])


@pytest.mark.parametrize("mode", ["subprocess", "persistent", "terminal"])
async def test_real_shell_group_enforces_descendant_writes(
    tmp_path: Path, mode: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    extra = tmp_path / 'extra \\ "root"'
    extra.mkdir()
    settings = SandboxSettings(
        backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(writable_roots=[extra]),
    )
    async with _agent(tmp_path, settings, mode) as assembled:
        workspace = tmp_path / 'workspace "quoted"'
        (workspace / ".git").mkdir()
        (extra / ".git").mkdir()
        (workspace / "escape").symlink_to(outside, target_is_directory=True)
        targets = [
            (workspace / "allowed.txt", "ALLOWED"),
            (extra / "allowed.txt", "ALLOWED"),
            (outside / "denied.txt", "DENIED"),
            (workspace / ".git" / "denied.txt", "DENIED"),
            (extra / ".git" / "denied.txt", "DENIED"),
            (workspace / "escape" / "denied.txt", "DENIED"),
        ]
        bash = assembled.tool_manager.get_tool("bash")
        assert bash is not None
        expected_names = ["bash"] if mode == "subprocess" else ["bash", "bash_input"]
        assert assembled.tool_manager.list_tools() == expected_names
        for target, expected in targets:
            target.write_text("original")  # The unsandboxed parent can write here.
            code = (
                "from pathlib import Path\n"
                "try:\n"
                f"    Path({str(target)!r}).write_text('changed')\n"
                "except PermissionError:\n"
                "    print('DENIED')\n"
                "else:\n"
                "    print('ALLOWED')\n"
            )
            result = await bash.execute(command=_python_command(code))
            assert expected in result, result
            assert target.read_text() == ("changed" if expected == "ALLOWED" else "original")


@pytest.mark.parametrize("network", [False, True])
@pytest.mark.parametrize("mode", ["subprocess", "persistent"])
async def test_real_shell_network_flag_controls_loopback(
    tmp_path: Path, network: bool, mode: str,
) -> None:
    settings = SandboxSettings(backend=SandboxBackend.LOCAL, network=network)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        async with _agent(tmp_path, settings, mode) as assembled:
            bash = assembled.tool_manager.get_tool("bash")
            assert bash is not None
            code = (
                "import socket\n"
                "try:\n"
                f"    s = socket.create_connection(('127.0.0.1', {port}), timeout=2)\n"
                "except PermissionError:\n"
                "    print('DENIED')\n"
                "else:\n"
                "    s.close()\n"
                "    print('CONNECTED')\n"
            )
            result = await bash.execute(command=_python_command(code))
            assert ("CONNECTED" if network else "DENIED") in result, result


@pytest.mark.parametrize("surface", [WriteSurface.NONE, WriteSurface.ROOTS, WriteSurface.FULL])
async def test_real_write_surface_modes(tmp_path: Path, surface: WriteSurface) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    outside = tmp_path / "outside.txt"
    settings = SandboxSettings(
        backend=SandboxBackend.LOCAL,
        exclusive=ExclusiveConfig(write_surface=surface, writable_roots=[extra]),
    )
    async with _agent(tmp_path, settings, "subprocess") as assembled:
        bash = assembled.tool_manager.get_tool("bash")
        assert bash is not None
        for target, allowed in (
            (tmp_path / 'workspace "quoted"' / "write.txt", surface is WriteSurface.FULL),
            (extra / "write.txt", surface is not WriteSurface.NONE),
            (outside, surface is WriteSurface.FULL),
        ):
            target.write_text("original")
            result = await bash.execute(command=_python_command(
                "from pathlib import Path\n"
                "try:\n"
                f"    Path({str(target)!r}).write_text('changed')\n"
                "except PermissionError:\n"
                "    print('DENIED')\n"
                "else:\n"
                "    print('ALLOWED')\n"
            ))
            assert ("ALLOWED" if allowed else "DENIED") in result, result
            assert target.read_text() == ("changed" if allowed else "original")


async def test_real_persistent_shell_retains_cwd_and_answers_input(tmp_path: Path) -> None:
    settings = SandboxSettings(backend=SandboxBackend.LOCAL)
    async with _agent(tmp_path, settings, "persistent") as assembled:
        bash = assembled.tool_manager.get_tool("bash")
        bash_input = assembled.tool_manager.get_tool("bash_input")
        assert bash is not None and bash_input is not None
        await bash.execute(command="mkdir child && cd child")
        cwd = await bash.execute(command="pwd")
        assert str(tmp_path / 'workspace "quoted"' / "child") in cwd

        waiting = await bash.execute(command="read -r -p 'Name: ' answer; printf '<%s>\\n' \"$answer\"")
        assert "[hint:" in waiting, waiting
        answer = await bash_input.execute(line="seatbelt-answer")
        assert "<seatbelt-answer>" in answer, answer
        assert "READY" in await bash.execute(command="printf READY")
