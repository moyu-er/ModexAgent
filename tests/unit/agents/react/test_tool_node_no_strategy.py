"""TDD: without SuspendStrategy, tools must execute normally — never cancelled."""
from unittest.mock import MagicMock, AsyncMock

import pytest
from framework.agents.react.nodes.tool import ToolNode
from framework.agents.react.agent import ReActAgent, ReActEvent
from framework.agents.react.constants import ReActMetaKey, ReActNode, ReActReason
from framework.agents.react.strategy import SuspendResumeStrategy
from framework.agents.react.strategy import InMemoryTurnResumeStateStore
from framework.runtime.enums import ApprovalDenyPolicy
from framework.approval.store import LocalFileApprovalStateStore
from framework.core.agent import AgentContext
from framework.core.tool_manager import InMemoryToolManager, ToolResult
from framework.core.types import LLMResponse, ToolCall
from framework.memory.history import ListMessageHistory
from framework.core.emitter import ContentEmitter
from framework.approval.constants import ApprovalDecision, ApprovalTier
from framework.agents.react.approval import TieredToolApprovalClassifier
from framework.approval.config import AgentApprovalConfig, ToolApprovalConfig


class _MockProvider:
    """Minimal mock — ToolNode doesn't call LLM."""


class _FakeEmitter(ContentEmitter):
    """Emitter that doesn't send anything."""
    event_enum = ReActEvent

    async def emit(self, event, data=None): pass
    async def emit_delta(self, delta: str): pass
    async def emit_content(self, content: str): pass
    async def emit_stream_end(self, *, resuming: bool = False): pass
    async def emit_complete(self, result): pass
    async def emit_error(self, error_msg: str): pass
    def wants_streaming(self) -> bool: return False


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
    async def test_no_approval_runtime_tools_execute(self):
        """No approval runtime → all tools NORMAL → execute directly."""
        agent = ReActAgent(_MockProvider(), mode="full")
        node = ToolNode(agent)
        ctx = make_ctx()
        ctx.runtime = MagicMock()
        ctx.runtime.approval = None
        ctx.runtime.hooks = None
        ctx.runtime.interceptors = None
        ctx.runtime.checkpoint_store = None
        ctx.runtime.control = None

        tool_call = ToolCall(tool_name="list_dir", call_id="1", arguments={"path": "/home"})
        ctx.metadata[ReActMetaKey.ITERATION] = 1
        ctx.metadata[ReActMetaKey.LLM_RESPONSE] = LLMResponse(content="ok", tool_calls=[tool_call])
        ctx.emitter = _FakeEmitter()
        real_execute = agent._execute_tool
        agent._execute_tool = AsyncMock(return_value=ToolResult(tool_name="list_dir", result="ok"))
        try:
            result = await node.execute(ctx)
            assert result.reason != ReActReason.TURN_CANCELLED.value
            assert result.target == ReActNode.LLM.value
        finally:
            agent._execute_tool = real_execute

    @pytest.mark.asyncio
    async def test_classifier_but_no_strategy_still_executes(self):
        """approval.classifier configured but approval.suspend_strategy=None → ALLOWED."""
        agent = ReActAgent(_MockProvider(), mode="full")
        node = ToolNode(agent)
        ctx = make_ctx()
        ctx.runtime = MagicMock()
        ctx.runtime.approval = MagicMock()

        # Use new API: classifier with config that marks edit_file as DANGEROUS
        config = AgentApprovalConfig(
            enabled=True,
            tools={"edit_file": ToolApprovalConfig(allowed_paths=[])},
        )
        ctx.runtime.approval.classifier = TieredToolApprovalClassifier(config=config)
        ctx.runtime.approval.default_deny_policy = ApprovalDenyPolicy.CANCEL_TURN
        ctx.runtime.approval.suspend_strategy = None  # <-- strategy now under approval
        ctx.runtime.hooks = None
        ctx.runtime.interceptors = None
        ctx.runtime.checkpoint_store = None
        ctx.runtime.control = None

        tool_call = ToolCall(tool_name="edit_file", call_id="1", arguments={"path": "./safe/file.txt"})
        ctx.metadata[ReActMetaKey.ITERATION] = 1
        ctx.metadata[ReActMetaKey.LLM_RESPONSE] = LLMResponse(content="ok", tool_calls=[tool_call])
        ctx.emitter = _FakeEmitter()
        real_execute = agent._execute_tool
        agent._execute_tool = AsyncMock(return_value=ToolResult(tool_name="edit_file", result="done"))
        try:
            result = await node.execute(ctx)
            assert result.reason != ReActReason.TURN_CANCELLED.value
            assert result.target == ReActNode.LLM.value
        finally:
            agent._execute_tool = real_execute


class TestToolNodeWithStrategyTriggersApproval:
    """When strategy IS configured on approval.suspend_strategy, approval must trigger."""

    @pytest.mark.asyncio
    async def test_strategy_on_approval_triggers_graph_interrupt(self, tmp_path):
        """approval.suspend_strategy configured → PENDING → GraphInterrupt."""
        agent = ReActAgent(_MockProvider(), mode="full")
        node = ToolNode(agent)
        ctx = make_ctx()
        ctx.runtime = MagicMock()
        ctx.runtime.approval = MagicMock()

        # Use new API: classifier with config that marks edit_file as DANGEROUS
        config = AgentApprovalConfig(
            enabled=True,
            tools={"edit_file": ToolApprovalConfig(allowed_paths=[])},
        )
        ctx.runtime.approval.classifier = TieredToolApprovalClassifier(config=config)
        ctx.runtime.approval.default_deny_policy = ApprovalDenyPolicy.CANCEL_TURN
        # Real strategy that will raise GraphInterrupt
        strategy = SuspendResumeStrategy(
            LocalFileApprovalStateStore(tmp_path / "approval"),
            InMemoryTurnResumeStateStore(),
        )
        ctx.runtime.approval.suspend_strategy = strategy  # <-- on approval!
        ctx.runtime.hooks = None
        ctx.runtime.interceptors = None
        ctx.runtime.checkpoint_store = None
        ctx.runtime.control = None
        ctx.runtime.injection_queue = None

        tool_call = ToolCall(tool_name="edit_file", call_id="1",
                            arguments={"path": "/etc/shadow"})
        ctx.metadata[ReActMetaKey.ITERATION] = 1
        ctx.metadata["_react_iteration"] = 1
        ctx.metadata[ReActMetaKey.ITERATION_MSGS] = []
        ctx.metadata[ReActMetaKey.LLM_RESPONSE] = LLMResponse(
            content="ok", tool_calls=[tool_call])
        ctx.emitter = _FakeEmitter()

        from framework.core.graph.interrupt import GraphInterrupt
        with pytest.raises(GraphInterrupt):
            await node.execute(ctx)
