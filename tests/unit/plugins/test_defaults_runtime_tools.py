from __future__ import annotations

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
from modex_agent.runtime.todo import TodoItem, TodoStore
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


@pytest.mark.parametrize("name", ["ast_grep_search", "ast_grep_replace"])
async def test_ast_grep_factory_is_registered_and_creates_actual_name(name: str) -> None:
    factory = _registry().resolve(ComponentSlot.TOOL, name)

    assert isinstance(factory, PrototypeFactory)
    assert factory.config_model is ToolConfig
    tool = await factory.create(ToolConfig(), _ctx(None))
    assert tool.name == name
    assert factory.probe().name == name
