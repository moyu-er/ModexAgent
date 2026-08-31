from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.plugins.abc import ComponentSlot, PrototypeFactory
from modex_agent.plugins.assembly.context import (
    AgentContext,
    PoolRuntimeDeps,
)
from modex_agent.plugins.defaults.capabilities.todo import TodoSupply
from modex_agent.plugins.defaults.tools import ToolConfig, register_default_tools
from modex_agent.plugins.loader import PluginRegistrationContext
from modex_agent.plugins.registry import ComponentRegistry
from modex_agent.runtime.store import TodoItem, TodoStore
from modex_agent.tools.aci.edit_tool import AciEditTool


class _TodoStore(TodoStore):
    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        return None

    async def get(self, session_id: str) -> list[TodoItem]:
        return []

    async def delete(self, session_id: str) -> None:
        return None


def _registry() -> ComponentRegistry:
    registry = ComponentRegistry()
    with PluginRegistrationContext(registry) as registration:
        register_default_tools(registration)
    return registry


def _ctx(todo_store: TodoStore | None) -> AgentContext:
    return AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(
            capability_supply=({"todo": TodoSupply(store=todo_store)} if todo_store else {})
        ),
        agent_name="probe-agent",
    )


@pytest.mark.parametrize("name", ["todo_write", "todo_read"])
def test_todo_factory_is_registered_with_frozen_empty_config(name: str) -> None:
    factory = _registry().resolve(ComponentSlot.TOOL, name)

    assert not isinstance(factory, PrototypeFactory)
    assert factory.config_model is ToolConfig
    assert factory.config_model.model_config.get("frozen") is True
    assert factory.config_model.model_config.get("extra") == "forbid"


@pytest.mark.parametrize("name", ["todo_write", "todo_read"])
async def test_todo_factory_creates_tool_from_pool_supply_store(name: str) -> None:
    store = _TodoStore()
    factory = _registry().resolve(ComponentSlot.TOOL, name)

    tool = await factory.create(ToolConfig(), _ctx(store))

    assert tool.name == name
    assert tool._store is store  # noqa: SLF001


@pytest.mark.parametrize("name", ["todo_write", "todo_read"])
async def test_todo_factory_missing_supply_has_actionable_error(name: str) -> None:
    factory = _registry().resolve(ComponentSlot.TOOL, name)

    with pytest.raises(ValueError, match=r"capability_supply\['todo'\].*\{todo: \{\}\}"):
        await factory.create(ToolConfig(), _ctx(None))


def test_aci_registered_under_distinct_name_plain_edit_wins() -> None:
    """ACI is opt-in: "edit" stays the plain EditFileTool; the ACI upgrade
    lives under "aci_edit" (SpecBuilder swaps the name when the roster
    selects the supplement). Regression anchor: the registry once
    resolved "edit" to AciEditTool unconditionally, silently forcing the
    upgrade on every pool."""
    registry = _registry()

    plain = registry.resolve(ComponentSlot.TOOL, "edit")
    assert not isinstance(plain.probe(), AciEditTool)

    aci = registry.resolve(ComponentSlot.TOOL, "aci_edit")
    assert isinstance(aci.probe(), AciEditTool)
    assert aci.probe().name == "edit"


@pytest.mark.asyncio
async def test_bash_factory_without_terminal_manager_yields_persistent_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No terminal manager → the bash slot is the persistent shell fallback
    (replaces the former SubprocessTool stateless fallback). POSIX-host
    behavior: the win32 guard (separate tests below) is forced on."""
    from modex_agent.tools.terminal.persistent_bash import PersistentBashTool

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported", lambda: True
    )
    factory = _registry().resolve(ComponentSlot.TOOL, "bash")

    tool = await factory.create(ToolConfig(), _ctx(None))

    assert isinstance(tool, PersistentBashTool)
    assert tool.name == "bash"


@pytest.mark.asyncio
async def test_bash_factory_fallback_returns_pool_runtime_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback returns ``pool_runtime.persistent_bash`` AS-IS so the
    roster-resolved bash IS the instance the strategy registered together
    with its ``bash_input`` companion (shared session — no fork)."""
    from modex_agent.tools.terminal.persistent_bash import PersistentBashTool

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported", lambda: True
    )
    factory = _registry().resolve(ComponentSlot.TOOL, "bash")
    pool_bash = PersistentBashTool(initial_cwd="/ws")
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(persistent_bash=pool_bash),
        agent_name="probe-agent",
    )

    tool = await factory.create(ToolConfig(), ctx)

    assert tool is pool_bash


@pytest.mark.asyncio
async def test_bash_factory_fallback_fresh_instance_uses_workspace_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone fallback (no strategy-built instance — subagents, bare
    contexts) spawns a fresh shell rooted at the pool's workspace."""
    from modex_agent.tools.terminal.persistent_bash import PersistentBashTool
    from modex_agent.tools.workspace_scoped import WorkspaceRootProvider

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported", lambda: True
    )
    factory = _registry().resolve(ComponentSlot.TOOL, "bash")
    root_provider = MagicMock(spec=WorkspaceRootProvider)
    root_provider.current.return_value = Path("/pool/workspace")
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(root_provider=root_provider),
        agent_name="probe-agent",
    )

    tool = await factory.create(ToolConfig(), ctx)

    assert isinstance(tool, PersistentBashTool)
    # v3: initial_cwd lives on the tool's PersistentShellManager (the
    # constructor forwards it there when no manager is supplied).
    assert tool.manager._initial_cwd == str(Path("/pool/workspace"))  # noqa: SLF001


@pytest.mark.asyncio
async def test_bash_factory_win32_fallback_yields_subprocess_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows-host regression: without a POSIX pty the bash slot must
    degrade to the stateless SubprocessTool (a PersistentBashTool here
    would break at the first bash call — pexpect cannot forkpty)."""
    from modex_agent.tools.terminal.subprocess_tool import SubprocessTool

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported", lambda: False
    )
    factory = _registry().resolve(ComponentSlot.TOOL, "bash")

    tool = await factory.create(ToolConfig(), _ctx(None))

    assert isinstance(tool, SubprocessTool)
    assert tool.name == "bash"


@pytest.mark.asyncio
async def test_bash_factory_win32_fallback_ignores_pool_runtime_persistent_bash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The win32 guard wins over a strategy-built persistent instance —
    that shell could never spawn, so SubprocessTool is returned regardless."""
    from modex_agent.tools.terminal.persistent_bash import PersistentBashTool
    from modex_agent.tools.terminal.subprocess_tool import SubprocessTool

    monkeypatch.setattr(
        "modex_agent.plugins.defaults.tools.persistent_bash_supported", lambda: False
    )
    factory = _registry().resolve(ComponentSlot.TOOL, "bash")
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(persistent_bash=PersistentBashTool()),
        agent_name="probe-agent",
    )

    tool = await factory.create(ToolConfig(), ctx)

    assert isinstance(tool, SubprocessTool)


@pytest.mark.asyncio
async def test_bash_factory_with_terminal_manager_shares_pool_registry() -> None:
    """Split-brain regression: the FW-resolved bash tool must carry the
    POOL registry from pool_runtime — the same instance the BIZ terminal
    trio uses. A private ProcessRegistry here previously forked the
    process bookkeeping so `process write` could never find running
    commands."""
    from modex_agent.tools.terminal import CommandTool, ProcessRegistry

    factory = _registry().resolve(ComponentSlot.TOOL, "bash")
    pool_registry = ProcessRegistry()
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(terminal_manager=MagicMock(), process_registry=pool_registry),
        agent_name="probe-agent",
    )

    tool = await factory.create(ToolConfig(), ctx)

    assert isinstance(tool, CommandTool)
    assert tool._registry is pool_registry  # noqa: SLF001


@pytest.mark.asyncio
async def test_bash_factory_terminal_manager_without_registry_raises() -> None:
    """Broken PoolAssembleStage invariant (manager without registry) must
    fail fast at assembly time, not fork a private registry."""
    factory = _registry().resolve(ComponentSlot.TOOL, "bash")
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(terminal_manager=MagicMock()),
        agent_name="probe-agent",
    )

    with pytest.raises(ValueError, match=r"process_registry"):
        await factory.create(ToolConfig(), ctx)


@pytest.mark.asyncio
async def test_process_factory_shares_pool_registry() -> None:
    from modex_agent.tools.terminal import ProcessRegistry, ProcessTool

    factory = _registry().resolve(ComponentSlot.TOOL, "process")
    pool_registry = ProcessRegistry()
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(terminal_manager=MagicMock(), process_registry=pool_registry),
        agent_name="probe-agent",
    )

    tool = await factory.create(ToolConfig(), ctx)

    assert isinstance(tool, ProcessTool)
    assert tool._registry is pool_registry  # noqa: SLF001


@pytest.mark.asyncio
async def test_terminal_factory_shares_pool_registry() -> None:
    from modex_agent.tools.terminal import ProcessRegistry, TerminalTool

    factory = _registry().resolve(ComponentSlot.TOOL, "terminal")
    pool_registry = ProcessRegistry()
    ctx = AgentContext(
        registry=MagicMock(),
        workspace_ctx=MagicMock(),
        pool_runtime=PoolRuntimeDeps(terminal_manager=MagicMock(), process_registry=pool_registry),
        agent_name="probe-agent",
    )

    tool = await factory.create(ToolConfig(), ctx)

    assert isinstance(tool, TerminalTool)
    assert tool._registry is pool_registry  # noqa: SLF001


@pytest.mark.parametrize("name", ["process", "terminal"])
async def test_trio_factories_without_terminal_raise_actionable_error(
    name: str,
) -> None:
    factory = _registry().resolve(ComponentSlot.TOOL, name)

    with pytest.raises(ValueError, match=r"terminal_manager.*use_terminal"):
        await factory.create(ToolConfig(), _ctx(None))


@pytest.mark.parametrize("name", ["ast_grep_search", "ast_grep_replace"])
async def test_ast_grep_factory_is_registered_and_creates_actual_name(name: str) -> None:
    factory = _registry().resolve(ComponentSlot.TOOL, name)

    assert isinstance(factory, PrototypeFactory)
    assert factory.config_model is ToolConfig
    tool = await factory.create(ToolConfig(), _ctx(None))
    assert tool.name == name
    assert factory.probe().name == name
