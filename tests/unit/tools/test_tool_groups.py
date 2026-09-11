from __future__ import annotations

import pytest

from modex_agent.core.tool_group import ToolGroup, ToolGroupResource
from modex_agent.core.tool_manager import Tool, ToolOrigin
from modex_agent.tools.filter import FilteredToolManager
from modex_agent.tools.graph_tool_preset import GraphToolPreset
from modex_agent.tools.manager import InMemoryToolManager


class _Tool(Tool):
    def __init__(self, name: str) -> None:
        super().__init__(name=name, description=name, parameters={"type": "object"})

    async def execute(self, **kwargs: object) -> str:
        return self.name


class _Resource(ToolGroupResource):
    async def aclose(self) -> None:
        return None


def _group(resource: ToolGroupResource | None = None) -> ToolGroup:
    return ToolGroup(
        anchor="shell",
        variant="persistent",
        tools=(_Tool("shell"), _Tool("shell_input")),
        resource=resource,
    )


def test_register_group_installs_members_as_one_resource_free_unit() -> None:
    manager = InMemoryToolManager()

    manager.register_group(_group(_Resource()), origin=ToolOrigin.CAPABILITY_DERIVED)

    assert manager.list_tools() == ["shell", "shell_input"]
    assert len(manager.tool_groups) == 1
    registered = manager.tool_groups[0]
    assert registered.anchor == "shell"
    assert registered.variant == "persistent"
    assert registered.tools == (
        manager.get_tool("shell"),
        manager.get_tool("shell_input"),
    )
    assert registered.resource is None


def test_register_group_rolls_back_every_member_on_collision() -> None:
    manager = InMemoryToolManager()
    blocker = _Tool("shell_input")
    manager.register(blocker, origin=ToolOrigin.LOCAL_TOOLS)

    with pytest.raises(ValueError, match="shell_input"):
        manager.register_group(_group(), origin=ToolOrigin.CAPABILITY_DERIVED)

    assert manager.list_tools() == ["shell_input"]
    assert manager.get_tool("shell_input") is blocker
    assert manager.get_tool("shell") is None
    assert manager.tool_groups == ()


def test_register_group_uses_origin_rank_for_every_member() -> None:
    manager = InMemoryToolManager()
    manager.register(_Tool("shell"), origin=ToolOrigin.PRESET)
    manager.register(_Tool("shell_input"), origin=ToolOrigin.PRESET)
    group = _group()

    manager.register_group(group, origin=ToolOrigin.CAPABILITY_DERIVED)

    assert manager.get_tool("shell") is group.tools[0]
    assert manager.get_tool("shell_input") is group.tools[1]
    assert manager.origin_of("shell") is ToolOrigin.CAPABILITY_DERIVED
    assert manager.origin_of("shell_input") is ToolOrigin.CAPABILITY_DERIVED


def test_scalar_registration_cannot_overwrite_group_member() -> None:
    manager = InMemoryToolManager()
    manager.register_group(_group(), origin=ToolOrigin.CAPABILITY_DERIVED)
    member = manager.get_tool("shell_input")

    with pytest.raises(ValueError, match="group 'shell'"):
        manager.register(_Tool("shell_input"), origin=ToolOrigin.LOCAL_TOOLS)
    manager.register(_Tool("shell_input"), origin=ToolOrigin.EXTERNAL)

    assert manager.get_tool("shell_input") is member


def test_unregistering_one_member_removes_the_whole_group() -> None:
    manager = InMemoryToolManager()
    manager.register_group(_group())

    assert manager.unregister("shell_input") is True

    assert manager.list_tools() == []
    assert manager.tool_groups == ()


def test_filtered_manager_expands_an_allowed_group_anchor() -> None:
    manager = InMemoryToolManager()
    manager.register_group(_group())

    filtered = FilteredToolManager(manager, allowed_tools=["shell"])

    assert filtered.list_tools() == ["shell", "shell_input"]
    assert [group.anchor for group in filtered.tool_groups] == ["shell"]


def test_filtered_manager_rejects_partial_group_policy() -> None:
    manager = InMemoryToolManager()
    manager.register_group(_group())

    with pytest.raises(ValueError, match="shell_input"):
        FilteredToolManager(manager, denied_tools=["shell_input"])


def test_failed_group_registration_does_not_expand_filter_policy() -> None:
    manager = InMemoryToolManager()
    manager.register(_Tool("shell_input"), origin=ToolOrigin.LOCAL_TOOLS)
    filtered = FilteredToolManager(manager, allowed_tools=["shell"])

    with pytest.raises(ValueError, match="shell_input"):
        filtered.register_group(_group(), origin=ToolOrigin.CAPABILITY_DERIVED)
    manager.unregister("shell_input")
    manager.register(_Tool("shell_input"))

    assert filtered.list_tools() == []


def test_graph_copy_borrows_group_tools_without_resource_ownership() -> None:
    manager = InMemoryToolManager()
    resource = _Resource()
    manager.register_group(_group(resource), origin=ToolOrigin.CAPABILITY_DERIVED)

    copied = GraphToolPreset([]).build_tool_manager(manager)

    assert copied.get_tool("shell") is manager.get_tool("shell")
    assert copied.get_tool("shell_input") is manager.get_tool("shell_input")
    assert len(copied.tool_groups) == 1
    assert copied.tool_groups[0].resource is None
    assert copied.origin_of("shell") is ToolOrigin.CAPABILITY_DERIVED


def test_graph_copy_excludes_a_whole_group_by_anchor() -> None:
    manager = InMemoryToolManager()
    manager.register_group(_group())

    copied = GraphToolPreset([], excluded_base_tools={"shell"}).build_tool_manager(manager)

    assert copied.list_tools() == []
    assert copied.tool_groups == ()


def test_graph_copy_rejects_partial_group_exclusion() -> None:
    manager = InMemoryToolManager()
    manager.register_group(_group())

    with pytest.raises(ValueError, match="shell_input"):
        GraphToolPreset(
            [], excluded_base_tools={"shell_input"}
        ).build_tool_manager(manager)
