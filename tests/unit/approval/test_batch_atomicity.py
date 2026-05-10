"""TDD: batch atomicity — if ANY tool is denied, ALL tools must be denied/preempted.

Scenario: 5 tools, 1&2 approved, 3=NORMAL (auto-allowed), 4 denied →
ALL tools denied/preempted, NONE execute.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.agents.react.constants import ReActMetaKey
from framework.agents.react.nodes.tool import ToolNode
from framework.approval.constants import ApprovalDecision, ApprovalStatus
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.core.agent import AgentContext
from framework.core.emitter import ContentEmitter, ToolCall
from framework.core.tool_manager import InMemoryToolManager, ToolResult
from framework.core.types import LLMResponse
from framework.memory.history import ListMessageHistory


class _FakeEmitter(ContentEmitter):
    event_enum = type("E", (), {})()

    async def emit(self, event, data=None): pass
    async def emit_delta(self, delta: str): pass
    async def emit_content(self, content: str): pass
    async def emit_stream_end(self, *, resuming: bool = False): pass
    async def emit_complete(self, result): pass
    async def emit_error(self, error_msg: str): pass
    def wants_streaming(self) -> bool: return False


def _make_mock_agent():
    agent = MagicMock()
    agent._execute_tool = AsyncMock(return_value=ToolResult(
        tool_name="mock", call_id="x", result="ok",
    ))
    agent._build_tool_message = MagicMock(return_value={
        "role": "tool", "tool_call_id": "x", "name": "mock", "content": "msg",
    })
    agent._save_checkpoint = AsyncMock()
    agent._drain_injections = AsyncMock(return_value=[])
    agent._save_denial_checkpoint = AsyncMock()
    return agent


class TestApprovalStateBatchAtomicity:
    """When DENIED is applied, previously ALLOWED tools must become PREEMPTED."""

    def _make_state(self):
        reqs = [
            ApprovalRequest("write_file", "c1", {"path": "/a"}, "dangerous", 1),
            ApprovalRequest("write_file", "c2", {"path": "/b"}, "dangerous", 1),
            ApprovalRequest("list_dir", "c3", {"path": "/safe"}, "normal", 1),
            ApprovalRequest("shell", "c4", {"cmd": "rm -rf /"}, "dangerous", 1),
            ApprovalRequest("write_file", "c5", {"path": "/c"}, "dangerous", 1),
        ]
        return ApprovalState(session_id="s1", requests=reqs)

    def test_partial_approval_then_deny_all_preempted(self):
        """Tools 1&2 approved, tool 3 NORMAL → auto-ALLOWED, tool 4 denied →
        ALL tools must be DENIED/PREEMPTED, including previously approved 1&2&3."""
        state = self._make_state()
        # Simulate: user approves tool 1, 2
        state.apply("c1", ApprovalDecision.ALLOWED)
        state.apply("c2", ApprovalDecision.ALLOWED)
        # Tool 3 is NORMAL → auto-ALLOWED
        state.apply("c3", ApprovalDecision.ALLOWED)
        # User denies tool 4
        state.apply("c4", ApprovalDecision.DENIED)

        assert state.status == ApprovalStatus.DENIED
        decisions = state.final_decisions()
        expected = [
            ApprovalDecision.PREEMPTED,  # was ALLOWED, now PREEMPTED
            ApprovalDecision.PREEMPTED,  # was ALLOWED, now PREEMPTED
            ApprovalDecision.PREEMPTED,  # was ALLOWED, now PREEMPTED
            ApprovalDecision.DENIED,     # explicitly denied
            ApprovalDecision.PREEMPTED,  # cascaded
        ]
        assert decisions == expected, (
            f"BUG: batch atomicity violation. "
            f"Previously ALLOWED tools must be PREEMPTED when any tool denied. "
            f"Expected {expected}, got {decisions}"
        )

    def test_explicit_deny_first_tool_preempts_all(self):
        """First tool denied → ALL tools preempted, even though none were approved."""
        state = self._make_state()
        state.apply("c1", ApprovalDecision.DENIED)

        decisions = state.final_decisions()
        assert decisions == [
            ApprovalDecision.DENIED,
            ApprovalDecision.PREEMPTED,
            ApprovalDecision.PREEMPTED,
            ApprovalDecision.PREEMPTED,
            ApprovalDecision.PREEMPTED,
        ], f"BUG: first tool deny should preempt all, got {decisions}"

    def test_auto_deny_preempts_previously_allowed(self):
        """Auto-deny (unrelated input) must also preempt previously ALLOWED tools."""
        state = self._make_state()
        state.apply("c1", ApprovalDecision.ALLOWED)
        state.apply("c2", ApprovalDecision.ALLOWED)
        state.apply("c3", ApprovalDecision.ALLOWED)
        # Auto-deny: simulate unrelated input → deny remaining PENDING
        state.apply("c4", ApprovalDecision.DENIED)
        # deny_reason set by pipeline
        state.deny_reason = 'unrelated input: "hello world"'

        decisions = state.final_decisions()
        assert decisions == [
            ApprovalDecision.PREEMPTED,  # was ALLOWED
            ApprovalDecision.PREEMPTED,  # was ALLOWED
            ApprovalDecision.PREEMPTED,  # was ALLOWED
            ApprovalDecision.DENIED,
            ApprovalDecision.PREEMPTED,
        ]

    def test_all_approved_works_normally(self):
        """When ALL tools are approved, all should be ALLOWED."""
        state = self._make_state()
        for req in state.requests:
            state.apply(req.tool_call_id, ApprovalDecision.ALLOWED)

        decisions = state.final_decisions()
        assert all(d == ApprovalDecision.ALLOWED for d in decisions)
        assert state.status == ApprovalStatus.APPROVED


class TestToolNodeBatchAtomicity:
    """ToolNode._execute_batch: when ALL tools denied/preempted, NONE execute."""

    @pytest.mark.asyncio
    async def test_all_denied_no_tool_executes(self):
        """All 5 tools denied/preempted → 0 real executions, 5 fake results."""
        from framework.agents.react.agent import ReActEvent

        agent = _make_mock_agent()
        node = ToolNode(agent)

        tcs = [
            ToolCall("write_file", "c1", {"path": "/a"}),
            ToolCall("write_file", "c2", {"path": "/b"}),
            ToolCall("list_dir", "c3", {"path": "/safe"}),
            ToolCall("shell", "c4", {"cmd": "rm -rf /"}),
            ToolCall("write_file", "c5", {"path": "/c"}),
        ]
        decisions = [
            ApprovalDecision.PREEMPTED,
            ApprovalDecision.PREEMPTED,
            ApprovalDecision.PREEMPTED,
            ApprovalDecision.DENIED,
            ApprovalDecision.PREEMPTED,
        ]

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session_id="s1",
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: True,
                "APPROVAL_DENY_REASON": 'unrelated input: "hello"',
            },
        )
        ctx.emitter = _FakeEmitter()
        ctx.emitter.event_enum = ReActEvent

        result = await node._execute_batch(tcs, decisions, ctx)

        # Agent._execute_tool must NEVER be called
        agent._execute_tool.assert_not_called()

        # 5 fake tool messages must be built
        assert agent._build_tool_message.call_count == 5

        # Turn should be cancelled
        assert result.target.value == "end"
        assert "turn_cancelled" in str(result.reason)

        # The explicitly DENIED tool should have error
        denied_call = agent._build_tool_message.call_args_list[3][0][0]
        assert "denied" in str(denied_call.error).lower()

    @pytest.mark.asyncio
    async def test_mixed_denied_still_executes_none(self):
        """Even with some ALLOWED in decisions, if any DENIED → all preempted
        at state level before reaching ToolNode. This test verifies ToolNode
        handles the fully-resolved decisions correctly."""
        from framework.agents.react.agent import ReActEvent

        agent = _make_mock_agent()
        node = ToolNode(agent)

        tcs = [
            ToolCall("write_file", "c1", {"path": "/a"}),
            ToolCall("shell", "c2", {"cmd": "ls"}),
        ]
        # After state.apply(), ALL are denied/preempted
        decisions = [ApprovalDecision.DENIED, ApprovalDecision.PREEMPTED]

        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session_id="s1",
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: True,
            },
        )
        ctx.emitter = _FakeEmitter()
        ctx.emitter.event_enum = ReActEvent

        result = await node._execute_batch(tcs, decisions, ctx)

        agent._execute_tool.assert_not_called()
        assert agent._build_tool_message.call_count == 2
        assert "turn_cancelled" in str(result.reason)
