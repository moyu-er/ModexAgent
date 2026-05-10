"""TDD: _detect_approval_command with strategy (SuspendResumeStrategy).

Bug 1: When strategy is active, approval commands (/approve, /deny) have
_is_approval_cmd=False, so they get saved to message history.
Bug 2: When strategy is active, non-approval user input during pending
approval does NOT auto-deny — the system hangs.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.approval.constants import ApprovalDecision
from framework.approval.state import ApprovalRequest, ApprovalState
from framework.approval.types import ApprovalAction
from framework.core.agent import AgentContext
from framework.core.emitter import ToolCall
from framework.core.tool_manager import InMemoryToolManager, ToolResult
from framework.core.types import InputMessage
from framework.memory.history import ListMessageHistory
from framework.pipeline.pipeline import AgentPipeline


def _make_pipeline(*, strategy, prebuilt_runtime=None):
    """Minimal pipeline with just the fields _detect_approval_command needs."""
    p = AgentPipeline(
        agent=MagicMock(),
        context_manager=MagicMock(),
        tool_manager=MagicMock(),
        input_adapter=MagicMock(),
        output_adapter=MagicMock(),
        prebuilt_runtime=prebuilt_runtime,
    )
    # Override the strategy path
    if prebuilt_runtime is None:
        p._prebuilt_runtime = MagicMock()
        p._prebuilt_runtime.approval = MagicMock()
        p._prebuilt_runtime.approval.suspend_strategy = strategy
    return p


def _pending_approval_state(session_id="s1"):
    req = ApprovalRequest(
        tool_name="write_file",
        tool_call_id="c1",
        arguments={"path": "/etc/hosts"},
        tier="dangerous",
        iteration=1,
    )
    return ApprovalState(session_id=session_id, requests=[req])


class TestDetectApprovalCommandWithStrategy:
    """Bug 1: _detect_approval_command with strategy must set _is_approval_cmd=True
    for /approve and /deny commands."""

    @pytest.mark.asyncio
    async def test_approve_cmd_sets_is_approval_cmd_with_strategy(self):
        """When strategy exists, /approve must be detected as approval command."""
        state = _pending_approval_state()
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(content="/approve", session_id="s1")

        is_cmd, returned_state = await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        assert is_cmd is True, (
            "BUG: /approve with strategy should set _is_approval_cmd=True "
            "so _assemble_context skips saving to history"
        )
        assert returned_state is state

    @pytest.mark.asyncio
    async def test_deny_cmd_sets_is_approval_cmd_with_strategy(self):
        """When strategy exists, /deny must be detected as approval command."""
        state = _pending_approval_state()
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(content="/deny", session_id="s1")

        is_cmd, returned_state = await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        assert is_cmd is True, (
            "BUG: /deny with strategy should set _is_approval_cmd=True "
            "so _assemble_context skips saving to history"
        )

    @pytest.mark.asyncio
    async def test_no_pending_approval_not_approval_cmd(self):
        """When no approval is pending, /approve is NOT an approval command."""
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=None)

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(content="/approve", session_id="s1")

        is_cmd, returned_state = await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        assert is_cmd is False
        assert returned_state is None


class TestDetectApprovalCommandNonApprovalInput:
    """Bug 2: Non-approval input during pending approval must auto-deny all tools."""

    @pytest.mark.asyncio
    async def test_unrelated_text_during_approval_auto_denies_with_strategy(self):
        """Non-approval user input during pending approval → auto-deny all tools."""
        state = _pending_approval_state()
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)
        strategy.save_approval_state = AsyncMock()

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(content="hello, tell me a story", session_id="s1")

        is_cmd, returned_state = await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        # Should NOT be treated as an approval command
        assert is_cmd is False
        # All tools should be auto-denied
        decisions = returned_state.final_decisions()
        assert decisions == [ApprovalDecision.DENIED], (
            f"BUG: non-approval input during approval should auto-deny, "
            f"got {decisions}"
        )

    @pytest.mark.asyncio
    async def test_unrelated_text_auto_deny_saves_state_with_strategy(self):
        """Auto-deny must persist the denied state via strategy.save_approval_state."""
        state = _pending_approval_state()
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)
        strategy.save_approval_state = AsyncMock()

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(content="good morning", session_id="s1")

        await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        strategy.save_approval_state.assert_called_once()
        saved_state = strategy.save_approval_state.call_args[0][0]
        assert saved_state.final_decisions() == [ApprovalDecision.DENIED]

    @pytest.mark.asyncio
    async def test_no_pending_approval_no_auto_deny(self):
        """When no approval is pending, unrelated text should NOT trigger auto-deny."""
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=None)

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(content="hello", session_id="s1")

        is_cmd, returned_state = await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        assert is_cmd is False
        assert returned_state is None

    @pytest.mark.asyncio
    async def test_auto_deny_stores_deny_reason_on_state_with_strategy(self):
        """Auto-deny must store truncated user input as deny_reason on ApprovalState."""
        state = _pending_approval_state()
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)
        strategy.save_approval_state = AsyncMock()

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(
            content="tell me a long and completely unrelated story about dragons",
            session_id="s1",
        )

        await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        saved_state = strategy.save_approval_state.call_args[0][0]
        assert saved_state.deny_reason is not None, (
            "BUG: auto-deny must set deny_reason on ApprovalState"
        )
        assert "unrelated input" in saved_state.deny_reason
        assert "tell me a long" in saved_state.deny_reason

    @pytest.mark.asyncio
    async def test_explicit_deny_does_not_set_deny_reason_with_strategy(self):
        """Explicit /deny should NOT set deny_reason — error differs from auto-deny."""
        state = _pending_approval_state()
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(content="/deny", session_id="s1")

        await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        # Explicit /deny: state is returned, caller handles via _handle_approval_command
        assert state.deny_reason is None, (
            "BUG: explicit /deny should not set deny_reason"
        )

    @pytest.mark.asyncio
    async def test_multi_tool_auto_deny_first_denied_rest_preempted_with_strategy(self):
        """Multi-tool: auto-deny → first DENIED with reason, rest PREEMPTED."""
        reqs = [
            ApprovalRequest("write_file", "c1", {"path": "/etc/a"}, "dangerous", 1),
            ApprovalRequest("write_file", "c2", {"path": "/etc/b"}, "dangerous", 1),
            ApprovalRequest("shell", "c3", {"command": "rm -rf /"}, "dangerous", 1),
        ]
        state = ApprovalState(session_id="s1", requests=reqs)
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)
        strategy.save_approval_state = AsyncMock()

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(content="random chatter", session_id="s1")

        await pipeline._approval.detect(msg, "s1", {}, prebuilt_runtime=pipeline._prebuilt_runtime)

        decisions = state.final_decisions()
        assert decisions == [
            ApprovalDecision.DENIED,
            ApprovalDecision.PREEMPTED,
            ApprovalDecision.PREEMPTED,
        ], f"BUG: first should be DENIED, rest PREEMPTED, got {decisions}"
        assert state.deny_reason is not None

    @pytest.mark.asyncio
    async def test_source_agent_during_approval_buffers_not_denies_with_strategy(self):
        """Peer agent messages during approval should buffer, NOT auto-deny."""
        state = _pending_approval_state()
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(
            content="peer response", session_id="s1",
            metadata={"source_agent": "peer1"},
        )

        is_cmd, returned_state = await pipeline._approval.detect(
            msg, "s1", {"source_agent": "peer1"},
        )

        assert is_cmd is False
        # Source agent messages should be buffered, not denied
        assert state.unresolved_count == 1, (
            "BUG: peer messages during approval should be buffered, not denied"
        )


class TestToolNodeDenyReasonError:
    """ToolNode._execute_batch must include deny_reason in error for DENIED tools."""

    @pytest.mark.asyncio
    async def test_denied_tool_includes_deny_reason_in_error(self):
        """When APPROVAL_DENY_REASON is in metadata, denied tool error includes it."""
        from framework.agents.react.agent import ReActAgent
        from framework.agents.react.constants import ReActMetaKey
        from framework.agents.react.nodes.tool import ToolNode
        from framework.approval.constants import ApprovalDecision
        from framework.core.types import LLMResponse
        from framework.core.emitter import ContentEmitter, ToolCall

        class _FakeEmitter(ContentEmitter):
            event_enum = type("E", (), {})()

            async def emit(self, event, data=None): pass
            async def emit_delta(self, delta: str): pass
            async def emit_content(self, content: str): pass
            async def emit_stream_end(self, *, resuming: bool = False): pass
            async def emit_complete(self, result): pass
            async def emit_error(self, error_msg: str): pass
            def wants_streaming(self) -> bool: return False

        agent = MagicMock()
        agent._execute_tool = AsyncMock(return_value=ToolResult(
            tool_name="mock_tool", call_id="c1", result="ok",
        ))
        agent._build_tool_message = MagicMock(return_value={
            "role": "tool", "tool_call_id": "c1", "name": "mock_tool", "content": "error msg",
        })
        agent._save_checkpoint = AsyncMock()
        agent._drain_injections = AsyncMock(return_value=[])
        agent._save_denial_checkpoint = AsyncMock()

        node = ToolNode(agent)
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session_id="s1",
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: False,
                "APPROVAL_DENY_REASON": 'unrelated input: "tell me a story"',
            },
        )
        ctx.emitter = _FakeEmitter()

        tc = ToolCall(tool_name="write_file", call_id="c1", arguments={"path": "/etc/hosts"})
        decisions = [ApprovalDecision.DENIED]

        from framework.agents.react.agent import ReActEvent
        ctx.emitter.event_enum = ReActEvent
        result = await node._execute_batch([tc], decisions, ctx)

        # The tool error message should indicate denied
        tool_msg = agent._build_tool_message.call_args[0][0]
        assert "denied" in str(tool_msg.error).lower(), (
            f"BUG: denied tool error should contain 'denied', got: {tool_msg.error}"
        )

    @pytest.mark.asyncio
    async def test_preempted_tool_does_not_include_deny_reason(self):
        """PREEMPTED tools (cascaded) should show preempted error."""
        from framework.agents.react.constants import ReActMetaKey
        from framework.agents.react.nodes.tool import ToolNode
        from framework.approval.constants import ApprovalDecision
        from framework.core.emitter import ContentEmitter, ToolCall

        class _FakeEmitter(ContentEmitter):
            event_enum = type("E", (), {})()

            async def emit(self, event, data=None): pass
            async def emit_delta(self, delta: str): pass
            async def emit_content(self, content: str): pass
            async def emit_stream_end(self, *, resuming: bool = False): pass
            async def emit_complete(self, result): pass
            async def emit_error(self, error_msg: str): pass
            def wants_streaming(self) -> bool: return False

        agent = MagicMock()
        agent._execute_tool = AsyncMock(return_value=ToolResult(
            tool_name="mock_tool", call_id="c2", result="ok",
        ))
        agent._build_tool_message = MagicMock(return_value={
            "role": "tool", "tool_call_id": "c2", "name": "mock_tool", "content": "preempted",
        })
        agent._save_checkpoint = AsyncMock()
        agent._drain_injections = AsyncMock(return_value=[])
        agent._save_denial_checkpoint = AsyncMock()

        node = ToolNode(agent)
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session_id="s1",
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: False,
            },
        )
        from framework.agents.react.agent import ReActEvent
        ctx.emitter = _FakeEmitter()
        ctx.emitter.event_enum = ReActEvent

        tc = ToolCall(tool_name="write_file", call_id="c2", arguments={"path": "/etc/b"})
        decisions = [ApprovalDecision.PREEMPTED]

        result = await node._execute_batch([tc], decisions, ctx)
        tool_msg = agent._build_tool_message.call_args[0][0]
        assert tool_msg.error == f"Error: {ApprovalDecision.PREEMPTED}", (
            f"BUG: PREEMPTED tool should have plain error, got: {tool_msg.error}"
        )

    @pytest.mark.asyncio
    async def test_explicit_deny_without_reason_uses_plain_error(self):
        """Explicit /deny (no deny_reason) → plain 'Error: denied'."""
        from framework.agents.react.constants import ReActMetaKey
        from framework.agents.react.nodes.tool import ToolNode
        from framework.approval.constants import ApprovalDecision
        from framework.core.emitter import ContentEmitter, ToolCall

        class _FakeEmitter(ContentEmitter):
            event_enum = type("E", (), {})()

            async def emit(self, event, data=None): pass
            async def emit_delta(self, delta: str): pass
            async def emit_content(self, content: str): pass
            async def emit_stream_end(self, *, resuming: bool = False): pass
            async def emit_complete(self, result): pass
            async def emit_error(self, error_msg: str): pass
            def wants_streaming(self) -> bool: return False

        agent = MagicMock()
        agent._execute_tool = AsyncMock()
        agent._build_tool_message = MagicMock(return_value={
            "role": "tool", "tool_call_id": "c3", "name": "mock_tool", "content": "denied",
        })
        agent._save_checkpoint = AsyncMock()
        agent._drain_injections = AsyncMock(return_value=[])
        agent._save_denial_checkpoint = AsyncMock()

        node = ToolNode(agent)
        ctx = AgentContext(
            system_prompt="test",
            history=ListMessageHistory(),
            tool_manager=InMemoryToolManager(),
            session_id="s1",
            metadata={
                ReActMetaKey.ITERATION: 1,
                ReActMetaKey.ITERATION_MSGS: [],
                ReActMetaKey.DENY_AS_CANCEL: False,
            },
        )
        from framework.agents.react.agent import ReActEvent
        ctx.emitter = _FakeEmitter()
        ctx.emitter.event_enum = ReActEvent

        tc = ToolCall(tool_name="write_file", call_id="c3", arguments={"path": "/etc/x"})
        decisions = [ApprovalDecision.DENIED]

        result = await node._execute_batch([tc], decisions, ctx)
        tool_msg = agent._build_tool_message.call_args[0][0]
        assert tool_msg.error == "Error: denied", (
            f"BUG: explicit deny should have plain error, got: {tool_msg.error}"
        )
        """Peer agent messages during approval should buffer, NOT auto-deny."""
        state = _pending_approval_state()
        strategy = MagicMock()
        strategy.load_approval_state = AsyncMock(return_value=state)

        pipeline = _make_pipeline(strategy=strategy)
        msg = InputMessage(
            content="peer response", session_id="s1",
            metadata={"source_agent": "peer1"},
        )

        is_cmd, returned_state = await pipeline._approval.detect(
            msg, "s1", {"source_agent": "peer1"},
        )

        assert is_cmd is False
        # Source agent messages should be buffered, not denied
        assert state.unresolved_count == 1, (
            "BUG: peer messages during approval should be buffered, not denied"
        )
