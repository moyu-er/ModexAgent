"""TDD: without SuspendStrategy, tools must execute normally — never cancelled."""
import pytest
from unittest.mock import MagicMock, AsyncMock
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.agent import ReActAgent, ReActEvent
from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.core.agent import AgentContext
from framework.core.tool_manager import InMemoryToolManager, ToolResult
from framework.core.types import LLMResponse, ToolCall
from framework.memory.history import ListMessageHistory
from framework.core.emitter import ContentEmitter
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.agents.react.approval import TieredToolApprovalClassifier


class _MockProvider:
    """Minimal mock — ToolNode doesn't call LLM."""


class _FakeEmitter(ContentEmitter):
    """Emitter that doesn't send anything."""
    event_enum = ReActEvent

    async def emit(self, event, data=None):
        pass

    async def emit_delta(self, delta: str):
        pass

    async def emit_content(self, content: str):
        pass

    async def emit_stream_end(self, *, resuming: bool = False):
        pass

    async def emit_complete(self, result):
        pass

    async def emit_error(self, error_msg: str):
        pass

    def wants_streaming(self) -> bool:
        return False


def make_ctx(**extras):
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session_id="s1",
        **extras,
    )


class TestToolNodeNoStrategyExecutesNormally:
    """Without SuspendStrategy, ALL tools must execute — never cancelled."""

    @pytest.mark.asyncio
    async def test_no_strategy_no_classifier_tools_execute(self):
        """No classifier + no strategy → all tools NORMAL → execute directly."""
        agent = ReActAgent(_MockProvider(), mode="full")
        node = ToolNode(agent)

        ctx = make_ctx()
        ctx.runtime = MagicMock()
        ctx.runtime.approval = None  # no approval runtime
        ctx.runtime.suspend_strategy = None
        ctx.runtime.hooks = None
        ctx.runtime.interceptors = None
        ctx.runtime.checkpoint_store = None
        ctx.runtime.control = None

        tool_call = ToolCall(tool_name="list_dir", call_id="1",
                             arguments={"path": "/home"})
        ctx.metadata[ReActMetaKey.ITERATION] = 1
        ctx.metadata[ReActMetaKey.LLM_RESPONSE] = LLMResponse(
            content="ok", tool_calls=[tool_call])
        ctx.emitter = _FakeEmitter()

        # Mock tool execution
        real_execute = agent._execute_tool
        agent._execute_tool = AsyncMock(return_value=ToolResult(
            tool_name="list_dir", result="files: a.txt"))

        try:
            result = await node.execute(ctx)
            # Must NOT be turn_cancelled
            assert result.target != ReActNode.END.value or \
                   result.reason != ReActReason.TURN_CANCELLED.value
            # Should route to LLM (tools done)
            assert result.target == ReActNode.LLM.value
        finally:
            agent._execute_tool = real_execute

    @pytest.mark.asyncio
    async def test_classifier_but_no_strategy_still_executes(self):
        """Classifier returns DANGEROUS but no strategy → ALLOWED → tools execute."""
        agent = ReActAgent(_MockProvider(), mode="full")
        node = ToolNode(agent)

        ctx = make_ctx()
        ctx.runtime = MagicMock()
        # Classifier IS configured (returns DANGEROUS for edit_file)
        ctx.runtime.approval = MagicMock()
        ctx.runtime.approval.classifier = TieredToolApprovalClassifier(
            dangerous=MagicMock(matches=lambda name: name == "edit_file"))
        ctx.runtime.approval.deny_as_cancel = True
        # But strategy is NOT configured
        ctx.runtime.suspend_strategy = None
        ctx.runtime.hooks = None
        ctx.runtime.interceptors = None
        ctx.runtime.checkpoint_store = None
        ctx.runtime.control = None

        tool_call = ToolCall(tool_name="edit_file", call_id="1",
                             arguments={"path": "./safe/file.txt"})
        ctx.metadata[ReActMetaKey.ITERATION] = 1
        ctx.metadata[ReActMetaKey.LLM_RESPONSE] = LLMResponse(
            content="ok", tool_calls=[tool_call])
        ctx.emitter = _FakeEmitter()

        real_execute = agent._execute_tool
        agent._execute_tool = AsyncMock(return_value=ToolResult(
            tool_name="edit_file", result="done"))

        try:
            result = await node.execute(ctx)
            assert result.reason != ReActReason.TURN_CANCELLED.value
            assert result.target == ReActNode.LLM.value
        finally:
            agent._execute_tool = real_execute
