"""Tests for workspace-scoped tool ownership of all agent types.

Verifies that:
- get_preset_tools wraps standard tools when given a root_provider.
- Subagent tool managers built inside a pool use the workspace root provider.
- Main-agent tools remain workspace-scoped.
- Reviewer/dream/summarizer data paths come from R.pool_data; the
  experience dir comes from the experience capability supply (SPEC §8.3).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bot.workspace.handle import (
    PoolWorkspaceResources,
    WorkspaceHandle,
)

_POOL_DECLARATION = """\
pool:
  name: test_pool
  agents:
    main:
      description: ownership test root
"""

from modex_agent.messaging.broker_memory import InMemoryMessageBroker
from modex_agent.persistence.adapters.file_session_store import LocalFileSessionStore
from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.tools.presets import ToolPreset, get_preset_tools
from modex_agent.tools.standard import ReadFileTool, SearchFilesTool
from modex_agent.tools.workspace_scoped import (
    WorkspaceRootProvider,
    WorkspaceScopedFileTool,
    WorkspaceScopedShellTool,
    WorkspaceScopedTool,
)
from modex_agent.workspace.context import WorkspaceContext

pytestmark = pytest.mark.skipif(
    shutil.which("modexctl") is None,
    reason="modexctl CLI not available",
)

# ── Helpers ───────────────────────────────────────────────────────────────


class _StaticRootProvider(WorkspaceRootProvider):
    """Test provider that returns a fixed path."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def current(self) -> Path:
        return self._root


def _build_test_resources(tmp_path: Path) -> PoolWorkspaceResources:
    """Build a minimal PoolWorkspaceResources for testing."""
    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path)
    broker = InMemoryMessageBroker()
    return PoolWorkspaceResources(
        target=target,
        ctx=ctx,
        overflow_store=LocalFileToolOverflowStore(workspace=ctx.paths.overflow_dir),
        session_index_store=LocalFileSessionStore(root=ctx.paths.session_index_dir),
        broker=broker,
    )


# ── Tests: get_preset_tools wrapping ────────────────────────────────────


def test_get_preset_tools_without_provider_returns_unwrapped() -> None:
    tools = get_preset_tools(ToolPreset.FULL)
    assert len(tools) == 6
    for t in tools:
        assert not isinstance(t, WorkspaceScopedTool)


def test_get_preset_tools_with_provider_wraps_file_tools(tmp_path: Path) -> None:
    provider = _StaticRootProvider(tmp_path)
    tools = get_preset_tools(ToolPreset.FULL, root_provider=provider)
    assert len(tools) == 6

    file_tools = [t for t in tools if t.name in {"read", "write", "edit", "ls", "glob", "grep"}]
    assert len(file_tools) == 6
    for t in file_tools:
        assert isinstance(t, WorkspaceScopedFileTool)


def test_get_preset_tools_with_provider_wraps_bash_tool() -> None:
    from modex_agent.tools.terminal import SubprocessTool

    provider = _StaticRootProvider(Path("/tmp/fake_ws"))

    def _make_bash() -> SubprocessTool:
        return SubprocessTool(timeout=300)

    tools = get_preset_tools(
        ToolPreset.FULL, subprocess_tool_factory=_make_bash, root_provider=provider
    )
    bash_tools = [t for t in tools if t.name == "bash"]
    assert len(bash_tools) == 1
    assert isinstance(bash_tools[0], WorkspaceScopedShellTool)


def test_get_preset_tools_none_preset_with_provider_returns_empty() -> None:
    provider = _StaticRootProvider(Path("/tmp/fake_ws"))
    tools = get_preset_tools(ToolPreset.NONE, root_provider=provider)
    assert tools == []


def test_get_preset_tools_read_only_with_provider_wraps_and_no_bash() -> None:
    provider = _StaticRootProvider(Path("/tmp/fake_ws"))
    tools = get_preset_tools(ToolPreset.READ_ONLY, root_provider=provider)
    assert all(isinstance(t, WorkspaceScopedTool) for t in tools)
    assert not any(t.name == "bash" for t in tools)


# ── Tests: main-agent tool workspace scoping ────────────────────────────


def test_main_agent_tools_wrapped_when_root_provider_given(tmp_path: Path) -> None:
    """Simulate _build_tools wrapping main-agent tools with a provider."""
    from modex_agent.tools.standard import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool

    provider = _StaticRootProvider(tmp_path)
    file_tools = [ReadFileTool(), WriteFileTool(), EditFileTool(), ListDirTool()]
    from modex_agent.tools.workspace_scoped import wrap_standard_tools

    wrapped = wrap_standard_tools(file_tools, provider)
    assert len(wrapped) == 4
    for t in wrapped:
        assert isinstance(t, WorkspaceScopedFileTool)


def test_main_agent_search_tools_wrapped_when_root_provider_given(tmp_path: Path) -> None:
    from modex_agent.tools.workspace_scoped import wrap_standard_tools

    provider = _StaticRootProvider(tmp_path)
    search_tools = [SearchFilesTool(), SearchFilesTool()]
    wrapped = wrap_standard_tools(search_tools, provider)
    for t in wrapped:
        assert isinstance(t, WorkspaceScopedFileTool)


# ── Tests: subagent tool manager uses workspace root provider ───────────


async def test_subagent_tool_manager_uses_workspace_root_provider(tmp_path: Path) -> None:
    """The subagent tool manager starts EMPTY: every tool — preset tools,
    the derived communication entries, per-agent MCP — registers on it by
    the roster road in ``assemble_native_agent`` (TOOL-slot factories
    reading the context chain), never by ``_build_tool_manager`` itself.
    Workspace scoping of the roster tools is asserted by the wrap tests
    above (``wrap_standard_tools`` + the materialize-path tests in the
    framework suite)."""
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.template import AgentTemplate
    from modex_agent.scope.spec import AgentSpec

    provider = _StaticRootProvider(tmp_path)
    deps = AgentMaterializeDeps(
        agent_factory=None,  # not used by _build_tool_manager
        pool=object(),  # type: ignore[arg-type]  # not used by _build_tool_manager
        session_factory=None,  # not used by _build_tool_manager
        broker=InMemoryMessageBroker(),
        tree=MagicMock(),
        root_provider=provider,
    )
    template = AgentTemplate(
        spec=AgentSpec(
            name="scout",
            toolset=ToolPreset.READ_ONLY,
            description="Test scout",
        ),
        toolset_profile=ToolPreset.READ_ONLY,
    )

    tm = await template._build_tool_manager(
        deps,
        "scout",
        runtime_dir=None,
        assembly_spec=MagicMock(mcp_servers=()),
        component_ctx=MagicMock(),
    )
    assert tm.list_tools() == []


# ── Tests: main-agent tool manager workspace scoping ────────────────────


async def test_main_agent_tool_manager_is_workspace_scoped(tmp_path: Path) -> None:
    """Verify that create_pool's tool_manager contains workspace-scoped tools when handle is given."""
    from bot.service.model_choice import ModelChoiceRegistry
    from bot.service.model_config import BotModelConfig
    from bot.service.pool import create_pool

    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.llm_struct import RuntimeSafetyPolicy
    from modex_agent.hook import HookRunner
    from modex_agent.interceptor.chain import InterceptorChain
    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.multi_agent import SessionRetentionPolicy
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps

    target = tmp_path / "ws"
    target.mkdir()

    assembly_deps = PoolAssemblyDeps(memory=MemoryConfig())

    broker = InMemoryMessageBroker()
    await broker.start()

    workspace_handle = WorkspaceHandle(target=target, data_root=target / ".modex")

    _yml = """
models:
  default_provider: "A"
  default_model: "M1"
  providers:
    - {key: a, name: "A", url: u, api_key: k, models: [{name: M1, model: openai/m1}]}
"""
    (target / "model.yml").write_text(_yml, encoding="utf-8")
    bot_model_config = BotModelConfig.from_yaml(target / "model.yml")

    from ...declaration_driver import build_declared

    pool_instance = await create_pool(
        pool_name="test_pool",
        declared=build_declared(
            _POOL_DECLARATION,
            project_dir=tmp_path,
            data_dir=target / ".modex",
            pool_name="test_pool",
        ),
        assembly_deps=assembly_deps,
        project_dir=tmp_path,
        workspace_registry=object(),
        workspace_resources=object(),
        data_dir=target / ".modex",
        broker=broker,
        output_adapter=object(),  # type: ignore[arg-type]
        safety=RuntimeSafetyPolicy(),
        retention=SessionRetentionPolicy(),
        im_ui=object(),  # type: ignore[arg-type]
        shared_hooks=[],
        shared_hook_runner=HookRunner(),
        shared_interceptor_chain=InterceptorChain(),
        control_channel=InMemoryControlChannel(),
        workspace_handle=workspace_handle,
        bot_model_config=bot_model_config,
        model_choice_registry=ModelChoiceRegistry(),
    )

    tm = pool_instance.tool_manager
    tool_names = tm.list_tools()
    assert len(tool_names) > 0

    # File tools should be workspace-scoped
    for name in {"read", "write", "edit", "ls", "find", "grep"}:
        if name in tool_names:
            tool = tm.get_tool(name)
            assert tool is not None
            assert isinstance(tool, WorkspaceScopedTool), f"{name} should be workspace-scoped"

    await broker.stop()


# ── Tests: PoolWorkspaceResources ownership ─────────────────────────────


async def test_pool_resources_experience_dir_from_capability_supply(tmp_path: Path) -> None:
    """The experience dir's construction authority is the experience
    capability supply (SPEC §8.3): ``ExperienceCapability.supply`` builds
    it at the workspace paths layout, keyed by the pool's root agent —
    pool_data carries no experience resource anymore."""
    from bot.workspace.pool_data import build_pool_data

    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.plugins.capability import PoolSupplyAgentEntry, PoolSupplyView
    from modex_agent.plugins.defaults.capabilities.experience import ExperienceCapability
    from modex_agent.scope.spec import AgentSpec

    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path)

    root_agent = AgentSpec(name="main")
    assembly_deps = PoolAssemblyDeps(memory=MemoryConfig())

    pool_data = await build_pool_data(ctx, "test_pool", root_agent, None, assembly_deps, "")
    # pool_data no longer carries the experience dir (the supply owns it).
    assert pool_data.experience_dir is None

    supply = ExperienceCapability().supply(
        PoolSupplyView(
            pool_name="test_pool",
            entries=(PoolSupplyAgentEntry(agent_name="main", config={}),),
            root_agent_name="main",
            data_dir=ctx.paths.root,
        )
    )
    # The supply's dir exactly matches the workspace paths accessor.
    assert supply.experience_dir == ctx.paths.experience_dir("test_pool", "main")


async def test_build_pool_data_uses_workspace_sqlite_for_session_memory(
    tmp_path: Path,
) -> None:
    from bot.workspace.pool_data import build_pool_data

    from modex_agent.ioc.configs.app import AppConfig
    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.memory.scope import MemoryContext
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.persistence.managers import WorkspacePersistenceManager
    from modex_agent.scope.spec import AgentSpec

    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path)
    root_agent = AgentSpec(name="main")
    persistence = WorkspacePersistenceManager(ctx.paths.root / "state.db")
    await persistence.open()
    try:
        pool_data = await build_pool_data(
            ctx,
            "test_pool",
            root_agent,
            None,
            PoolAssemblyDeps(memory=MemoryConfig()),
            "",
            app_config=AppConfig(),
            persistence=persistence,
        )
        memory_system = pool_data.context_manager.memory_system
        assert memory_system is not None
        from modex_agent.persistence.adapters.turn_state_store import SqliteTurnStateStore
        from modex_agent.persistence.coordinator import SqliteDecisionCoordinator

        assert isinstance(pool_data.decision_coordinator, SqliteDecisionCoordinator)
        assert isinstance(pool_data.turn_store, SqliteTurnStateStore)
        assert pool_data.decision_coordinator._connection is persistence.connection
        assert (
            pool_data.decision_coordinator._codec_registry is pool_data.turn_store._codec_registry
        )
        mem_ctx = MemoryContext(session_id="session-1", agent_id="main")
        history = memory_system.create_message_history(mem_ctx)
        await history.append({"role": "user", "content": "persisted in SQLite"})

        row_count = await persistence.connection.query_value(
            "SELECT COUNT(*) FROM memory_session_messages",
            int,
        )
        assert row_count == 1

        await memory_system.close()
        table_count = await persistence.connection.query_value(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
            int,
        )
        assert table_count > 0
    finally:
        await persistence.close()


async def test_build_pool_data_file_backend_has_no_decision_coordinator(
    tmp_path: Path,
) -> None:
    from bot.workspace.pool_data import build_pool_data

    from modex_agent.ioc.configs.app import AppConfig
    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.persistence.config import PersistenceBackend, PersistenceConfig
    from modex_agent.scope.spec import AgentSpec

    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path)
    root_agent = AgentSpec(name="main")

    pool_data = await build_pool_data(
        ctx,
        "test_pool",
        root_agent,
        None,
        PoolAssemblyDeps(memory=MemoryConfig()),
        "",
        app_config=AppConfig(
            persistence=PersistenceConfig(backend=PersistenceBackend.FILE),
        ),
    )

    assert pool_data.decision_coordinator is None
    memory_system = pool_data.context_manager.memory_system
    assert memory_system is not None
    await memory_system.close()


async def test_pool_resources_background_tasks_live_on_r(tmp_path: Path) -> None:
    """Verify that BackgroundTaskRunner is attached to R and cancels on stop."""
    from bot.workspace.background import BackgroundTaskRunner

    r = _build_test_resources(tmp_path)
    r.background = BackgroundTaskRunner(
        pool_data={},
        assembly_deps={},
        default_pool_name=None,
    )
    assert r.background is not None

    # Manually inject a mock dream engine and curator to verify task lifecycle
    import asyncio

    async def _mock_loop() -> None:
        while True:
            await asyncio.sleep(10)

    r.background.dream_engine = object()  # type: ignore[assignment]
    r.background._tasks.append(asyncio.create_task(_mock_loop(), name="workspace-dream"))

    # Verify tasks are registered on R.background
    assert len(r.background.tasks) > 0
    task_names = {t.get_name() for t in r.background.tasks}
    assert any("dream" in n for n in task_names), f"Expected dream task in {task_names}"

    # Stop should cancel all tasks
    await r.background.stop()
    assert len(r.background.tasks) == 0
    for t in r.background.tasks:
        assert t.done()


# The retired "curator tasks on R.background" test died with the
# experience capability's supply face: the curator loop lives on
# ``ExperienceSupply`` now — pool assembly starts it and pool teardown
# (``AgentPool.shutdown_all``) stops it (SPEC §8.3 D4). The lifecycle is
# pinned in the framework suite (``tests/unit/plugins/test_experience_supply.py``).


# ── Tests: workspace-scoped path resolution ─────────────────────────────


def test_workspace_scoped_tool_rewrites_relative_path(tmp_path: Path) -> None:
    provider = _StaticRootProvider(tmp_path)
    inner = ReadFileTool()
    scoped = WorkspaceScopedFileTool(inner, provider)

    args = scoped._scoped_args({"path": "foo/bar.txt"})
    assert args["path"] == str(tmp_path / "foo" / "bar.txt")


def test_workspace_scoped_tool_rewrites_dot_path(tmp_path: Path) -> None:
    provider = _StaticRootProvider(tmp_path)
    inner = ReadFileTool()
    scoped = WorkspaceScopedFileTool(inner, provider)

    args = scoped._scoped_args({"path": "."})
    assert args["path"] == str(tmp_path)


def test_workspace_scoped_tool_leaves_absolute_path_alone(tmp_path: Path) -> None:
    provider = _StaticRootProvider(tmp_path)
    inner = ReadFileTool()
    scoped = WorkspaceScopedFileTool(inner, provider)

    # A path that is absolute on the CURRENT platform must be left untouched.
    # A bare "/etc/passwd" is not absolute on Windows (no drive), so the scoping
    # logic correctly treats it as relative there — use a platform-absolute path.
    absolute = "C:/Windows/System32" if os.name == "nt" else "/etc/passwd"
    args = scoped._scoped_args({"path": absolute})
    assert args["path"] == absolute
