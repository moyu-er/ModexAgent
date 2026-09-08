"""Unit tests for core/tool_manager.py + tools/manager.py (C2).

Verify Tool, InMemoryToolManager, ToolResult behaviors including
registration, execution, and schema generation.
"""

import pytest

from modex_agent.core.tool_manager import (
    Tool,
    ToolConfig,
    ToolResult,
)
from modex_agent.tools.manager import InMemoryToolManager


class _DummyTool(Tool):
    """A dummy tool for testing."""

    @property
    def name(self) -> str:
        return "dummy"

    @property
    def description(self) -> str:
        return "Does nothing"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "value": {"type": "integer"},
            },
            "required": ["value"],
        }

    async def execute(self, value: int):
        return value * 2


class _AsyncDummyTool(Tool):
    """Async dummy tool."""

    @property
    def name(self) -> str:
        return "async_dummy"

    @property
    def description(self) -> str:
        return "Async echo"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "msg": {"type": "string"},
            },
        }

    async def execute(self, msg: str = ""):
        return f"echo: {msg}"


class _FailingTool(Tool):
    """Tool that always fails."""

    @property
    def name(self) -> str:
        return "failing"

    @property
    def description(self) -> str:
        return "Always fails"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}}

    async def execute(self):
        raise RuntimeError("intended failure")


# ---------------------------------------------------------------------------
# ToolResult
# ---------------------------------------------------------------------------

class TestToolResult:
    @pytest.mark.asyncio
    async def test_success_property(self):
        tr = ToolResult.from_text("t", "ok")
        assert tr.success is True
        assert tr.error is None

    @pytest.mark.asyncio
    async def test_failure_property(self):
        tr = ToolResult(tool_name="t", error="bad")
        assert tr.success is False

    @pytest.mark.asyncio
    async def test_to_dict_roundtrip(self):
        tr = ToolResult.from_text("t", "ok", execution_time=1.5, call_id="c1")
        d = tr.to_dict()
        assert d["tool_name"] == "t"
        assert d["error"] is None
        assert d["execution_time"] == 1.5
        assert d["call_id"] == "c1"
        assert d["success"] is True
        assert d["content"] == [{"type": "text", "text": "ok"}]

    @pytest.mark.asyncio
    async def test_to_message_format(self):
        tr = ToolResult.from_text("t", "ok", call_id="c1")
        msg = tr.to_message()
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "c1"
        assert msg["name"] == "t"
        assert msg["content"] == "ok"

    @pytest.mark.asyncio
    async def test_to_message_on_error(self):
        tr = ToolResult(tool_name="t", error="fail", call_id="c1")
        msg = tr.to_message()
        assert "Error: fail" in msg["content"]


# ---------------------------------------------------------------------------
# Tool schema / validation
# ---------------------------------------------------------------------------

class TestToolSchemaAndValidation:
    def test_get_schema(self):
        t = _DummyTool()
        schema = t.get_schema()
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "dummy"
        assert "value" in schema["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# InMemoryToolManager registration
# ---------------------------------------------------------------------------

class TestInMemoryToolManagerRegistration:
    @pytest.fixture
    def tm(self):
        return InMemoryToolManager()

    def test_register_and_list(self, tm):
        t = _DummyTool()
        tm.register(t)
        assert "dummy" in tm.list_tools()

    def test_unregister(self, tm):
        t = _DummyTool()
        tm.register(t)
        assert tm.unregister("dummy") is True
        assert tm.unregister("dummy") is False

    def test_get_tool(self, tm):
        t = _DummyTool()
        tm.register(t)
        assert tm.get_tool("dummy") is t
        assert tm.get_tool("missing") is None

    def test_contains(self, tm):
        tm.register(_DummyTool())
        assert "dummy" in tm
        assert "missing" not in tm

    def test_get_tool_descriptions(self, tm):
        tm.register(_DummyTool())
        descs = tm.get_tool_descriptions()
        assert len(descs) == 1
        assert descs[0]["function"]["name"] == "dummy"

    def test_disabled_tool_excluded_from_descriptions(self, tm):
        t = _DummyTool()
        t.config = ToolConfig(enabled=False)
        tm.register(t)
        assert tm.get_tool_descriptions() == []


# ---------------------------------------------------------------------------
# InMemoryToolManager execution
# ---------------------------------------------------------------------------

class TestInMemoryToolManagerExecution:
    @pytest.fixture
    def tm(self):
        return InMemoryToolManager()

    @pytest.mark.asyncio
    async def test_execute_simple(self, tm):
        tm.register(_DummyTool())
        result = await tm.execute("dummy", {"value": 5})
        assert result.success is True
        assert result.message_content() == "10"

    @pytest.mark.asyncio
    async def test_execute_tool_not_found(self, tm):
        result = await tm.execute("missing", {})
        assert result.success is False
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_execute_disabled_tool(self, tm):
        t = _DummyTool()
        t.config = ToolConfig(enabled=False)
        tm.register(t)
        result = await tm.execute("dummy", {"value": 1})
        assert result.success is False
        assert "disabled" in result.error.lower()

    @pytest.mark.asyncio
    async def test_execute_failure(self, tm):
        tm.register(_FailingTool())
        result = await tm.execute("failing", {})
        assert result.success is False
        assert "intended failure" in result.error
