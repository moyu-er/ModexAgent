"""Unit tests for core/tool_manager.py.

TDD: verify Tool, InMemoryToolManager, FunctionalTool, ToolResult
behaviors including registration, execution, batch execution,
parameter validation, and schema generation.
"""

import pytest

from framework.core.tool_manager import (
    FunctionalTool,
    InMemoryToolManager,
    Tool,
    ToolResult,
)


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
        tr = ToolResult(tool_name="t", result="ok")
        assert tr.success is True
        assert tr.error is None

    @pytest.mark.asyncio
    async def test_failure_property(self):
        tr = ToolResult(tool_name="t", error="bad")
        assert tr.success is False

    @pytest.mark.asyncio
    async def test_to_dict_roundtrip(self):
        tr = ToolResult(tool_name="t", result="ok", execution_time=1.5, call_id="c1")
        d = tr.to_dict()
        assert d["tool_name"] == "t"
        assert d["result"] == "ok"
        assert d["execution_time"] == 1.5
        assert d["call_id"] == "c1"
        assert d["success"] is True

    @pytest.mark.asyncio
    async def test_to_message_format(self):
        tr = ToolResult(tool_name="t", result="ok", call_id="c1")
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

    @pytest.mark.asyncio
    async def test_register_and_list(self, tm):
        await tm.startup()
        t = _DummyTool()
        tm.register(t)
        assert "dummy" in tm.list_tools()
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_unregister(self, tm):
        await tm.startup()
        t = _DummyTool()
        tm.register(t)
        assert tm.unregister("dummy") is True
        assert tm.unregister("dummy") is False
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_get_tool(self, tm):
        await tm.startup()
        t = _DummyTool()
        tm.register(t)
        assert tm.get_tool("dummy") is t
        assert tm.get_tool("missing") is None
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_contains(self, tm):
        await tm.startup()
        tm.register(_DummyTool())
        assert "dummy" in tm
        assert "missing" not in tm
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_has_tool_compat(self, tm):
        await tm.startup()
        tm.register(_DummyTool())
        assert tm.has_tool("dummy") is True
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_get_tool_descriptions(self, tm):
        await tm.startup()
        tm.register(_DummyTool())
        descs = tm.get_tool_descriptions()
        assert len(descs) == 1
        assert descs[0]["function"]["name"] == "dummy"
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_disabled_tool_excluded_from_descriptions(self, tm):
        await tm.startup()
        t = _DummyTool()
        t.config.enabled = False
        tm.register(t)
        assert tm.get_tool_descriptions() == []
        await tm.shutdown()


# ---------------------------------------------------------------------------
# InMemoryToolManager execution
# ---------------------------------------------------------------------------

class TestInMemoryToolManagerExecution:
    @pytest.fixture
    def tm(self):
        return InMemoryToolManager()

    @pytest.mark.asyncio
    async def test_execute_simple(self, tm):
        await tm.startup()
        tm.register(_DummyTool())
        result = await tm.execute("dummy", {"value": 5})
        assert result.success is True
        assert result.result == 10
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_execute_tool_not_found(self, tm):
        await tm.startup()
        result = await tm.execute("missing", {})
        assert result.success is False
        assert "not found" in result.error.lower()
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_execute_disabled_tool(self, tm):
        await tm.startup()
        t = _DummyTool()
        t.config.enabled = False
        tm.register(t)
        result = await tm.execute("dummy", {"value": 1})
        assert result.success is False
        assert "disabled" in result.error.lower()
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_execute_failure(self, tm):
        await tm.startup()
        tm.register(_FailingTool())
        result = await tm.execute("failing", {})
        assert result.success is False
        assert "intended failure" in result.error
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_execute_batch_parallel(self, tm):
        tm.config.parallel_max_workers = 5
        await tm.startup()
        tm.register(_DummyTool())
        calls = [
            {"tool_name": "dummy", "arguments": {"value": i}}
            for i in range(3)
        ]
        results = await tm.execute_batch(calls, parallel=True)
        assert len(results) == 3
        assert [r.result for r in results] == [0, 2, 4]
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_execute_batch_sequential(self, tm):
        await tm.startup()
        tm.register(_DummyTool())
        calls = [
            {"tool_name": "dummy", "arguments": {"value": i}}
            for i in range(3)
        ]
        results = await tm.execute_batch(calls, parallel=False)
        assert len(results) == 3
        assert [r.result for r in results] == [0, 2, 4]
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_execute_empty_batch(self, tm):
        await tm.startup()
        results = await tm.execute_batch([])
        assert results == []
        await tm.shutdown()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with InMemoryToolManager() as tm:
            tm.register(_DummyTool())
            result = await tm.execute("dummy", {"value": 3})
            assert result.result == 6


# ---------------------------------------------------------------------------
# FunctionalTool
# ---------------------------------------------------------------------------

class TestFunctionalTool:
    def test_sync_function_wrap(self):
        def add(a: int, b: int) -> int:
            return a + b

        ft = FunctionalTool(
            name="add",
            description="Add two numbers",
            parameters={"type": "object", "properties": {}},
            func=add,
        )
        assert ft.name == "add"

    @pytest.mark.asyncio
    async def test_async_function_wrap(self):
        async def greet(name: str) -> str:
            return f"hi {name}"

        ft = FunctionalTool(
            name="greet",
            description="Greet",
            parameters={"type": "object", "properties": {}},
            func=greet,
        )
        result = await ft.execute(name="world")
        assert result == "hi world"

    @pytest.mark.asyncio
    async def test_sync_function_wrap_execution(self):
        def mul(a: int, b: int) -> int:
            return a * b

        ft = FunctionalTool(
            name="mul",
            description="Multiply",
            parameters={"type": "object", "properties": {}},
            func=mul,
        )
        result = await ft.execute(a=3, b=4)
        assert result == 12
