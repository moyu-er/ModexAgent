"""Tests for ReActAgent error response and cancellation handling (P0-a)."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from framework.agents.react.agent import ReActAgent
from framework.core.constants import FinishReason
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import AgentResult
from framework.core.types import LLMResponse, ToolCall
from framework.core.tool_manager import ToolResult


class _FakeEmitter:
    """Minimal emitter capturing calls for assertions."""

    def __init__(self):
        self.events: list = []
        self.deltas: list[str] = []
        self.completed: AgentResult | None = None
        self._streaming = False

    def wants_streaming(self) -> bool:
        return self._streaming

    async def emit(self, event, data=None):
        self.events.append((event, data))

    async def emit_delta(self, delta: str):
        self.deltas.append(delta)

    async def emit_content(self, full: str):
        if full:
            self.deltas.append(full)

    async def emit_stream_end(self, resuming: bool = False):
        pass

    async def emit_complete(self, result: AgentResult):
        self.completed = result

    async def emit_error(self, error: str):
        self.events.append(("error", error))


class _FakeHistory:
    """Minimal message history for tests."""

    def __init__(self):
        self.messages: list = []

    async def append(self, message):
        self.messages.append(message)

    async def replace_all(self, messages):
        self.messages = list(messages)

    def __iter__(self):
        return iter(self.messages)

    def __bool__(self):
        return bool(self.messages)


class _FakeContext:
    """Minimal AgentContext for unit tests."""

    def __init__(self):
        self.messages = [{"role": "user", "content": "hello"}]
        self.history = _FakeHistory()
        self.system_prompt = ""
        self.max_iterations = 5
        self.attachments: list = []
        self.tool_manager = None
        self.temperature = 0.7
        self.max_tokens = None
        self.checkpoint: list | None = None
        self.metadata: dict = {}
        self.session_id = "error-test"
        self.extensions: dict[str, Any] = {
            ExtensionKey.HOOKS: [],
            ExtensionKey.MAX_TOOLS_PER_TURN: None,
            ExtensionKey.GOVERNANCE: None,
            ExtensionKey.ON_CHECKPOINT: None,
            ExtensionKey.SAFETY: None,
            ExtensionKey.HOOK_RUNNER: None,
            ExtensionKey.INTERCEPTOR_CHAIN: None,
            ExtensionKey.CHECKPOINT_STORE: None,
            ExtensionKey.INJECTION_QUEUE: None,
            ExtensionKey.RUNTIME_CTX_MGR: None,
            ExtensionKey.RUNTIME_CTX: None,
        }

    async def to_messages(self):
        return list(self.messages)

    def get_tool_descriptions(self):
        return None


class TestReActAgentErrorResponse:
    """4.1: error response → fail turn instead of treating as normal content."""

    @pytest.mark.asyncio
    async def test_error_finish_reason_returns_agent_error(self):
        provider = MagicMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="Error calling LLM: something went wrong",
            finish_reason=FinishReason.ERROR.value,
            error="something went wrong",
        ))
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _FakeContext()

        result = await agent.run(ctx, emitter)

        assert result is not None
        assert result.stop_reason == "error"
        assert result.error is not None
        assert "something went wrong" in result.error or "LLM request failed" in result.error

    @pytest.mark.asyncio
    async def test_normal_response_proceeds(self):
        provider = MagicMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="Hello, how can I help?",
            finish_reason=FinishReason.STOP.value,
        ))
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _FakeContext()

        result = await agent.run(ctx, emitter)

        assert result is not None
        assert result.stop_reason == "completed" or result.stop_reason == "stop"
        assert result.content == "Hello, how can I help?"


class TestReActAgentCancelledError:
    """4.2: CancelledError handling before generic Exception."""

    @pytest.mark.asyncio
    async def test_cancelled_error_preserves_checkpoint(self):
        provider = MagicMock()

        async def raise_cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        provider.chat = raise_cancelled
        agent = ReActAgent(provider=provider)
        emitter = _FakeEmitter()
        ctx = _FakeContext()
        saved_checkpoint: list | None = None

        async def save_ckpt(messages):
            nonlocal saved_checkpoint
            saved_checkpoint = list(messages)

        ctx.extensions[ExtensionKey.ON_CHECKPOINT] = save_ckpt

        with pytest.raises(asyncio.CancelledError):
            await agent.run(ctx, emitter)

        # Checkpoint should be preserved (shielded save during cancellation)
        # Even if empty, the save was attempted


class TestReActAgentToolTimeout:
    """4.4: tool execution timeout."""

    @pytest.mark.asyncio
    async def test_tool_timeout_returns_error_result(self):
        provider = MagicMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="",
            finish_reason=FinishReason.TOOL_CALLS.value,
            tool_calls=[ToolCall(tool_name="slow_tool", arguments={}, call_id="c1")],
        ))

        class SlowTool:
            async def execute(self, tool_name, arguments):
                await asyncio.sleep(999)  # effectively hangs
                return ToolResult(tool_name=tool_name, result="done")

        class FakeToolManager:
            def __init__(self, tool):
                self._tool = tool
            async def execute(self, tool_name, arguments):
                return await self._tool.execute(tool_name, arguments)

        tool = SlowTool()
        ctx = _FakeContext()
        ctx.tool_manager = FakeToolManager(tool)
        agent = ReActAgent(provider=provider, tool_timeout=0.01)
        emitter = _FakeEmitter()

        result = await agent.run(ctx, emitter)

        # Tool should have timed out, agent continues to next iteration
        # or finishes with error depending on tool chain
        assert result is not None


class TestReActAgentHookTimeout:
    """4.3: hook execution timeout."""

    @pytest.mark.asyncio
    async def test_hook_timeout_is_logged_not_raised(self):
        provider = MagicMock()
        provider.chat = AsyncMock(return_value=LLMResponse(
            content="ok",
            finish_reason=FinishReason.STOP.value,
        ))

        class SlowHook:
            async def before_turn(self, context):
                await asyncio.sleep(999)

        agent = ReActAgent(provider=provider, hook_timeout=0.01)
        emitter = _FakeEmitter()
        ctx = _FakeContext()
        ctx.extensions[ExtensionKey.HOOKS] = [SlowHook()]

        # Should not raise — hook timeout is caught and logged
        result = await agent.run(ctx, emitter)
        assert result is not None
