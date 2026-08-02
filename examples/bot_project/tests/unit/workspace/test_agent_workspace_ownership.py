"""Tests for workspace-scoped tool ownership of all agent types.

Verifies that:
- get_preset_tools wraps standard tools when given a root_provider.
- Subagent tool managers built inside a pool use the workspace root provider.
- Main-agent tools remain workspace-scoped.
- Reviewer/curator/dream/summarizer data paths come from R.pool_data.
"""

from __future__ import annotations

import os
from pathlib import Path

from bot.workspace.handle import (
    PoolWorkspaceResources,
    WorkspaceHandle,
)

from modex_agent.core.session_store import LocalFileSessionStore
from modex_agent.messaging.broker_memory import InMemoryMessageBroker
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

    tools = get_preset_tools(ToolPreset.FULL, subprocess_tool_factory=_make_bash, root_provider=provider)
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
    """Verify that AgentTemplate._build_tool_manager passes root_provider to
    get_preset_tools (the tool-manager build moved here from
    AgentCommunicationService in ADR-0015 D5)."""
    from modex_agent.multi_agent.materialize_deps import AgentMaterializeDeps
    from modex_agent.multi_agent.pool_config.specs import SubagentSpec
    from modex_agent.multi_agent.template import AgentTemplate

    provider = _StaticRootProvider(tmp_path)
    deps = AgentMaterializeDeps(
        agent_factory=None,  # not used by _build_tool_manager
        pool=object(),  # type: ignore[arg-type]  # not used by _build_tool_manager
        session_factory=None,  # not used by _build_tool_manager
        broker=InMemoryMessageBroker(),
        root_provider=provider,
    )
    template = AgentTemplate(
        spec=SubagentSpec(
            agent_name="scout",
            tool_preset=ToolPreset.READ_ONLY,
            description="Test scout",
        ),
    )

    tm = await template._build_tool_manager(deps, "scout", runtime_dir=None)
    tools = tm.list_tools()
    assert len(tools) > 0
    for name in tools:
        tool = tm.get_tool(name)
        assert tool is not None
        if tool.name in {"read", "grep", "find", "ls"}:
            assert isinstance(tool, WorkspaceScopedFileTool), f"{tool.name} should be scoped"


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
    from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec

    target = tmp_path / "ws"
    target.mkdir()

    pool_spec = PoolSpec(
        name="test_pool",
        main_agent_name="main",
        main=MainAgentSpec(agent_name="main"),
    )
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

    pool_instance = await create_pool(
        pool_name="test_pool",
        pool_spec=pool_spec,
        assembly_deps=assembly_deps,
        project_dir=tmp_path,
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


async def test_pool_resources_experience_dir_from_pool_data(tmp_path: Path) -> None:
    """Verify that experience_dir comes from pool_data, not hard-coded paths."""
    from bot.workspace.pool_data import build_pool_data

    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec

    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path)

    pool_spec = PoolSpec(
        name="test_pool",
        main_agent_name="main",
        main=MainAgentSpec(agent_name="main"),
    )
    assembly_deps = PoolAssemblyDeps(memory=MemoryConfig())

    pool_data = await build_pool_data(ctx, "test_pool", pool_spec, None, assembly_deps, "")
    # experience_dir should exactly match the workspace paths accessor
    expected = ctx.paths.experience_dir("test_pool", "main")
    assert pool_data.experience_dir == expected


async def test_build_pool_data_uses_workspace_sqlite_for_session_memory(
    tmp_path: Path,
) -> None:
    from bot.workspace.pool_data import build_pool_data

    from modex_agent.core.scope import MemoryContext
    from modex_agent.ioc.configs.app import AppConfig
    from modex_agent.ioc.configs.memory import MemoryConfig
    from modex_agent.multi_agent.pool_config.deps import PoolAssemblyDeps
    from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
    from modex_agent.persistence.managers import WorkspacePersistenceManager

    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path)
    pool_spec = PoolSpec(
        name="test_pool",
        main_agent_name="main",
        main=MainAgentSpec(agent_name="main"),
    )
    persistence = WorkspacePersistenceManager(ctx.paths.root / "state.db")
    await persistence.open()
    try:
        pool_data = await build_pool_data(
            ctx,
            "test_pool",
            pool_spec,
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
            pool_data.decision_coordinator._codec_registry
            is pool_data.turn_store._codec_registry
        )
        await memory_system.add_messages(
            MemoryContext(session_id="session-1", agent_id="main"),
            [{"role": "user", "content": "persisted in SQLite"}],
        )

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
    from modex_agent.multi_agent.pool_config.specs import MainAgentSpec, PoolSpec
    from modex_agent.persistence.config import PersistenceBackend, PersistenceConfig

    target = tmp_path / "ws"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=tmp_path)
    pool_spec = PoolSpec(
        name="test_pool",
        main_agent_name="main",
        main=MainAgentSpec(agent_name="main"),
    )

    pool_data = await build_pool_data(
        ctx,
        "test_pool",
        pool_spec,
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
    r.background._tasks.append(
        asyncio.create_task(_mock_loop(), name="workspace-dream")
    )

    # Verify tasks are registered on R.background
    assert len(r.background.tasks) > 0
    task_names = {t.get_name() for t in r.background.tasks}
    assert any("dream" in n for n in task_names), f"Expected dream task in {task_names}"

    # Stop should cancel all tasks
    await r.background.stop()
    assert len(r.background.tasks) == 0
    for t in r.background.tasks:
        assert t.done()


async def test_pool_resources_background_tasks_curator_on_r(tmp_path: Path) -> None:
    """Verify that curator tasks are registered on R.background and cancel on stop."""
    from bot.workspace.background import BackgroundTaskRunner

    from modex_agent.core.experience.curator import ExperienceCurator
    from modex_agent.core.experience.meta import PerFileExperienceMetaStore

    r = _build_test_resources(tmp_path)

    # Create a mock curator
    exp_dir = tmp_path / "experiences"
    exp_dir.mkdir()
    meta = PerFileExperienceMetaStore(lambda: exp_dir)
    curator = ExperienceCurator(
        experience_dir=exp_dir,
        meta_store=meta,
        max_experiences=10,
    )

    r.background = BackgroundTaskRunner(
        pool_data={},
        assembly_deps={},
        default_pool_name=None,
    )
    r.background.curators["test_pool"] = curator
    r.background._curator_intervals["test_pool"] = 1

    # Start should create curator tasks
    await r.background.start()
    assert len(r.background.tasks) > 0
    task_names = {t.get_name() for t in r.background.tasks}
    assert any("curator" in n for n in task_names), f"Expected curator task in {task_names}"

    # Stop should cancel all tasks
    await r.background.stop()
    assert len(r.background.tasks) == 0


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
