"""Tests for ToolAssemblyKit."""

from __future__ import annotations

import pytest

from framework.core.tool_manager import InMemoryToolManager, Tool, ToolConfig
from framework.multi_agent.assembly_kit import ToolAssemblyKit


class DummyTool(Tool):
    """A simple tool without clone."""

    def __init__(self, name: str) -> None:
        super().__init__(name=name, description="dummy", parameters={}, config=ToolConfig())

    async def execute(self, **kwargs) -> str:
        return "ok"


class CloneableTool(Tool):
    """A tool that implements clone()."""

    def __init__(self, name: str, counter: int = 0) -> None:
        self.counter = counter
        super().__init__(name=name, description="cloneable", parameters={}, config=ToolConfig())

    async def execute(self, **kwargs) -> str:
        return f"count={self.counter}"

    def clone(self) -> CloneableTool:
        copied = CloneableTool(self.name, self.counter)
        return copied


@pytest.fixture
def source_manager():
    tm = InMemoryToolManager()
    tm.register(DummyTool("tool_a"))
    tm.register(CloneableTool("tool_b", counter=42))
    tm.register(DummyTool("tool_c"))
    return tm


class TestToolAssemblyKitAssemble:
    def test_assemble_copies_named_tools(self, source_manager):
        target = ToolAssemblyKit.assemble(source_manager, ["tool_a", "tool_b"])
        descs = target.get_tool_descriptions()
        names = {d["function"]["name"] for d in descs}
        assert names == {"tool_a", "tool_b"}

    def test_assemble_ignores_missing_names_silently(self, source_manager):
        target = ToolAssemblyKit.assemble(source_manager, ["tool_a", "missing"])
        descs = target.get_tool_descriptions()
        names = {d["function"]["name"] for d in descs}
        assert names == {"tool_a"}

    def test_assemble_uses_clone_when_available(self, source_manager):
        original = source_manager.get_tool("tool_b")
        assert isinstance(original, CloneableTool)
        target = ToolAssemblyKit.assemble(source_manager, ["tool_b"])
        copied = target.get_tool("tool_b")
        assert copied is not original
        assert isinstance(copied, CloneableTool)
        assert copied.counter == original.counter

    def test_assemble_shares_reference_when_no_clone(self, source_manager):
        original = source_manager.get_tool("tool_a")
        target = ToolAssemblyKit.assemble(source_manager, ["tool_a"])
        copied = target.get_tool("tool_a")
        assert copied is original


class TestToolAssemblyKitFilter:
    def test_filter_selects_by_predicate(self, source_manager):
        target = ToolAssemblyKit.filter(
            source_manager, lambda t: isinstance(t, CloneableTool)
        )
        descs = target.get_tool_descriptions()
        names = {d["function"]["name"] for d in descs}
        assert names == {"tool_b"}

    def test_filter_empty_when_no_match(self, source_manager):
        target = ToolAssemblyKit.filter(source_manager, lambda t: t.name == "none")
        assert target.get_tool_descriptions() == []

    def test_filter_uses_clone_when_available(self, source_manager):
        original = source_manager.get_tool("tool_b")
        target = ToolAssemblyKit.filter(source_manager, lambda t: t.name == "tool_b")
        copied = target.get_tool("tool_b")
        assert copied is not original

    def test_filter_shares_reference_when_no_clone(self, source_manager):
        original = source_manager.get_tool("tool_a")
        target = ToolAssemblyKit.filter(source_manager, lambda t: t.name == "tool_a")
        copied = target.get_tool("tool_a")
        assert copied is original
