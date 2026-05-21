"""Tests for SubagentAutoSendHook.

Covers:
- Auto-forwarding when agent forgets to call send_message_async
- Skipping when RuntimeContext records a communication tool call
- Skipping when content is empty
- before_turn is a no-op (clearing is done by ReActAgent)
- Content sanitization strips LLM reasoning tags
- Peer 3-part and pool 2-part session_id handling
"""

from unittest.mock import AsyncMock, MagicMock

from framework.core.agent import AgentContext
from framework.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices
from framework.core.emitter import AgentResult
from framework.core.runtime_context import InMemoryRuntimeContext, RuntimeContextManager
from framework.core.tool_manager import ToolManager
from framework.memory.history import ListMessageHistory
from framework.hook.builtin import SubagentAutoSendHook


class TestSubagentAutoSendHook:
    """Verify after_turn auto-forward logic with RuntimeContext-based detection."""

    def _make_bus(self):
        bus = MagicMock()
        bus.send = AsyncMock()
        return bus

    def _make_ctx(self, history_entries, session_id="conv_001:main", runtime_mgr=None):
        history = ListMessageHistory(list(history_entries))
        identity = TurnIdentity(agent_id="test", session_id=session_id, turn_id="t1")
        state = TurnStateBase(identity=identity, agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING)
        services = AgentRuntimeServices(runtime_context_manager=runtime_mgr)
        return AgentContext(
            system_prompt="",
            history=history,
            tool_manager=MagicMock(spec=ToolManager),
            session_id=session_id,
            runtime=AgentRuntime(services=services, state=state),
            identity=identity,
        )

    # ------------------------------------------------------------------
    # 1. Auto-forward when no communication tool was called
    # ------------------------------------------------------------------

    async def test_auto_sends_when_no_tool_call(self):
        """Content exists and no send_message in context → bus.send is called."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        mgr = RuntimeContextManager()
        ctx = self._make_ctx([], runtime_mgr=mgr)
        result = AgentResult(content="Task completed successfully.")

        # RuntimeContext is empty (no tool calls recorded)
        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        inbox_key, envelope = args
        assert inbox_key == "conv_001:main"
        assert envelope.payload["content"] == "Task completed successfully."
        assert envelope.source.name == "office-expert"
        assert envelope.target.name == "main"

    # ------------------------------------------------------------------
    # 2. Skip when RuntimeContext has a communication tool call
    # ------------------------------------------------------------------

    async def test_skips_when_send_message_async_recorded(self):
        """send_message_async was recorded → bus.send is NOT called."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        mgr = RuntimeContextManager()
        ctx = self._make_ctx([], runtime_mgr=mgr)
        result = AgentResult(content="Already sent via tool.")

        rc = await mgr.get_context("conv_001:main")
        await rc.record_tool_call("send_message_async", {"target_agent": "main"}, "ok")

        await hook.after_turn(ctx, result)
        bus.send.assert_not_awaited()

    async def test_skips_when_send_message_recorded(self):
        """send_message was recorded → bus.send is NOT called."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        mgr = RuntimeContextManager()
        ctx = self._make_ctx([], runtime_mgr=mgr)
        result = AgentResult(content="Already sent via tool.")

        rc = await mgr.get_context("conv_001:main")
        await rc.record_tool_call("send_message", {"target_agent": "main"}, "ok")

        await hook.after_turn(ctx, result)
        bus.send.assert_not_awaited()

    # ------------------------------------------------------------------
    # 3. Auto-forward when only non-communication tools were called
    # ------------------------------------------------------------------

    async def test_auto_forwards_with_non_comm_tools(self):
        """search tool was called but not send_message → bus.send IS called."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        mgr = RuntimeContextManager()
        ctx = self._make_ctx([], runtime_mgr=mgr)
        result = AgentResult(content="Search results...")

        rc = await mgr.get_context("conv_001:main")
        await rc.record_tool_call("search", {"q": "foo"}, "results")

        await hook.after_turn(ctx, result)
        bus.send.assert_awaited_once()

    # ------------------------------------------------------------------
    # 4. Skip when content is empty
    # ------------------------------------------------------------------

    async def test_skips_when_content_empty(self):
        """Result content is empty → bus.send is NOT called."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([])
        result = AgentResult(content="")

        await hook.after_turn(ctx, result)
        bus.send.assert_not_awaited()

    # ------------------------------------------------------------------
    # 5. Skip when result is None
    # ------------------------------------------------------------------

    async def test_skips_when_result_none(self):
        """Result is None → bus.send is NOT called."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([])

        await hook.after_turn(ctx, None)
        bus.send.assert_not_awaited()

    # ------------------------------------------------------------------
    # 6. before_turn is a no-op (clearing done by ReActAgent)
    # ------------------------------------------------------------------

    async def test_before_turn_is_noop(self):
        """before_turn does nothing; it exists for hook interface compliance."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([])

        # Should not raise
        await hook.before_turn(ctx)

    # ------------------------------------------------------------------
    # 7. Auto-forward works after context is cleared (new turn)
    # ------------------------------------------------------------------

    async def test_auto_forwards_after_context_cleared(self):
        """Simulate: tool sends → context cleared → new turn auto-forwards."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        mgr = RuntimeContextManager()
        ctx = self._make_ctx([], runtime_mgr=mgr)

        # Turn 1: tool sends
        rc = await mgr.get_context("conv_001:main")
        await rc.record_tool_call("send_message_async", {"to": "main"}, "ok")
        result1 = AgentResult(content="Sent via tool.")
        await hook.after_turn(ctx, result1)
        bus.send.assert_not_awaited()

        # Turn 2: context cleared (simulating ReActAgent._clear_runtime_context)
        await rc.clear()
        result2 = AgentResult(content="No tool this turn.")
        await hook.after_turn(ctx, result2)
        bus.send.assert_awaited_once()

    # ------------------------------------------------------------------
    # 8. Content sanitization strips think tags
    # ------------------------------------------------------------------

    async def test_sanitizes_think_tags(self):
        """Auto-forwarded content has <think/> tags stripped."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([])
        result = AgentResult(
            content="<think\nLLM reasoning here\n</think\n任务完成了！文档已创建。"
        )

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        _, envelope = args
        assert "<think" not in envelope.payload["content"]
        assert "LLM reasoning here" not in envelope.payload["content"]
        assert "任务完成了！文档已创建。" in envelope.payload["content"]

    # ------------------------------------------------------------------
    # 9. Content sanitization strips multiple tag types
    # ------------------------------------------------------------------

    async def test_sanitizes_multiple_tag_types(self):
        """Strip <think/>, <reasoning/>, <reflection/> tags."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([])
        result = AgentResult(
            content="<reasoning>step 1</reasoning><think\ndepth analysis</think\nFinal answer."
        )

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        _, envelope = args
        content = envelope.payload["content"]
        assert "<reasoning>" not in content
        assert "</reasoning>" not in content
        assert "<think" not in content
        assert "step 1" not in content
        assert "depth analysis" not in content
        assert "Final answer." in content

    # ------------------------------------------------------------------
    # 10. Peer session: agent_session_id routes to main's user session
    # ------------------------------------------------------------------

    async def test_peer_session_preserves_agent_session_id(self):
        """Peer session → agent_session_id = main_session(conv) (routes to main's user session)."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        peer_session = "conv_001:office-expert"
        ctx = self._make_ctx([], session_id=peer_session)
        result = AgentResult(content="Peer task done.")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        _, envelope = args
        assert envelope.agent_session_id == "conv_001:main"

    # ------------------------------------------------------------------
    # 11. Peer session: inbox_key is main's user session (2-part)
    # ------------------------------------------------------------------

    async def test_peer_session_inbox_key_is_two_part(self):
        """inbox_key for inbox delivery is always {cid}:main (2-part)."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([], session_id="conv_001:office-expert")
        result = AgentResult(content="Done.")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        inbox_key, _ = args
        assert inbox_key == "conv_001:main"
        assert inbox_key.count(":") == 1

    # ------------------------------------------------------------------
    # 12. Peer session: conversation_id extracted correctly
    # ------------------------------------------------------------------

    async def test_peer_session_conversation_id_extracted(self):
        """conversation_id is extracted from the session via strategy.parse()."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([], session_id="user_abc:office-expert")
        result = AgentResult(content="Done.")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        _, envelope = args
        assert envelope.conversation_id == "user_abc"

    # ------------------------------------------------------------------
    # 13. Pool session: routes to main's user session
    # ------------------------------------------------------------------

    async def test_pool_session_preserves_agent_session_id(self):
        """2-part pool session → agent_session_id = main_session(conv)."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        pool_session = "conv_001:office-expert"
        ctx = self._make_ctx([], session_id=pool_session)
        result = AgentResult(content="Done.")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        inbox_key, envelope = args
        assert envelope.agent_session_id == "conv_001:main"
        assert inbox_key == "conv_001:main"

    async def test_non_default_parent_name_uses_correct_session(self):
        """parent_name != 'main' → inbox_key uses correct parent name."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="qq_bot")
        ctx = self._make_ctx([], session_id="conv_001:office-expert")
        result = AgentResult(content="Done.")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        inbox_key, envelope = args
        assert inbox_key == "conv_001:qq_bot"
        assert envelope.agent_session_id == "conv_001:qq_bot"
        assert envelope.target.name == "qq_bot"
