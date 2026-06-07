"""Tests for SubagentAutoSendHook.

Covers:
- Auto-forwarding when agent forgets to call send_to_agent
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
        """Content exists and no send_to_agent in context → bus.send is called."""
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
        assert "<agent_result" in envelope.payload["content"]
        assert 'source="office-expert"' in envelope.payload["content"]
        assert 'status="completed"' in envelope.payload["content"]
        assert "Task completed successfully." in envelope.payload["content"]
        assert envelope.source.name == "office-expert"
        assert envelope.target.name == "main"

    # ------------------------------------------------------------------
    # 2. Skip when RuntimeContext has a communication tool call
    # ------------------------------------------------------------------

    async def test_skips_when_send_to_agent_async_recorded(self):
        """send_to_agent was recorded → bus.send is NOT called (legacy name compat)."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        mgr = RuntimeContextManager()
        ctx = self._make_ctx([], runtime_mgr=mgr)
        result = AgentResult(content="Already sent via tool.")

        rc = await mgr.get_context("conv_001:main")
        await rc.record_tool_call("send_to_agent", {"target_agent": "main"}, "ok")

        await hook.after_turn(ctx, result)
        bus.send.assert_not_awaited()

    async def test_skips_when_send_to_agent_recorded(self):
        """send_to_agent was recorded → bus.send is NOT called."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        mgr = RuntimeContextManager()
        ctx = self._make_ctx([], runtime_mgr=mgr)
        result = AgentResult(content="Already sent via tool.")

        rc = await mgr.get_context("conv_001:main")
        await rc.record_tool_call("send_to_agent", {"target_agent": "main"}, "ok")

        await hook.after_turn(ctx, result)
        bus.send.assert_not_awaited()

    # ------------------------------------------------------------------
    # 3. Auto-forward when only non-communication tools were called
    # ------------------------------------------------------------------

    async def test_auto_forwards_with_non_comm_tools(self):
        """search tool was called but not send_to_agent → bus.send IS called."""
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
        await rc.record_tool_call("send_to_agent", {"to": "main"}, "ok")
        result1 = AgentResult(content="Sent via tool.")
        await hook.after_turn(ctx, result1)
        bus.send.assert_not_awaited()

        # Turn 2: context cleared (simulating ReActAgent._clear_runtime_context)
        # Also clear _communicated so the hook treats this as a fresh cycle.
        await rc.clear()
        hook._communicated.discard(ctx.session_id)
        result2 = AgentResult(content="No tool this turn.")
        await hook.after_turn(ctx, result2)
        bus.send.assert_awaited_once()

    # ------------------------------------------------------------------
    # 8. Content sanitization strips think tags
    # ------------------------------------------------------------------

    async def test_sanitizes_think_tags(self):
        """Auto-forwarded content has <think/> tags stripped inside XML wrapper."""
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
        content = envelope.payload["content"]
        assert "<think" not in content
        assert "LLM reasoning here" not in content
        assert "任务完成了！文档已创建。" in content
        assert "<agent_result" in content

    # ------------------------------------------------------------------
    # 9. Content sanitization strips multiple tag types
    # ------------------------------------------------------------------

    async def test_sanitizes_multiple_tag_types(self):
        """Strip <think/>, <reasoning/>, <reflection/> tags inside XML wrapper."""
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
        assert "<agent_result" in content

    # ------------------------------------------------------------------
    # 10. Subagent session: agent_session_id routes to main's user session
    # ------------------------------------------------------------------

    async def test_subagent_session_preserves_agent_session_id(self):
        """Subagent session → agent_session_id = main_session(conv) (routes to main's user session)."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        subagent_session = "conv_001:office-expert"
        ctx = self._make_ctx([], session_id=subagent_session)
        result = AgentResult(content="Subagent task done.")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        args, _ = bus.send.await_args
        _, envelope = args
        assert envelope.agent_session_id == "conv_001:main"

    # ------------------------------------------------------------------
    # 11. Subagent session: inbox_key is main's user session (2-part)
    # ------------------------------------------------------------------

    async def test_subagent_session_inbox_key_is_two_part(self):
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
    # 12. Subagent session: conversation_id extracted correctly
    # ------------------------------------------------------------------

    async def test_subagent_session_conversation_id_extracted(self):
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

    # ------------------------------------------------------------------
    # 14. Skip auto-forward when stop_reason is max_iterations
    # ------------------------------------------------------------------

    async def test_auto_forwards_when_max_iterations(self):
        """stop_reason=max_iterations → bus.send IS called.
        The subagent may have produced partial output that the parent needs to see,
        even if it hit its step limit. The actual stop_reason is forwarded so
        the parent knows why the subagent stopped."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([])
        result = AgentResult(content="agent ran out of steps", stop_reason="max_iterations")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()

    # ------------------------------------------------------------------
    # 15. Still auto-forwards with normal stop reason (no regression)
    # ------------------------------------------------------------------

    async def test_still_auto_forwards_when_normal_stop_reason(self):
        """stop_reason='completed' (not max_iterations) → auto-forward still fires."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([])
        result = AgentResult(content="Task done.", stop_reason="completed")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()

    async def test_still_auto_forwards_when_no_stop_reason(self):
        """stop_reason is None (default) → auto-forward still fires."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        ctx = self._make_ctx([])
        result = AgentResult(content="Task done.")

        await hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()

    # ------------------------------------------------------------------
    # 16. Default hook is safe — no-op when agent_bus is None
    # ------------------------------------------------------------------

    async def test_noop_when_agent_bus_is_none(self):
        """Default constructor (agent_bus=None) → after_turn is a no-op, no crash."""
        hook = SubagentAutoSendHook()
        ctx = self._make_ctx([])
        result = AgentResult(content="Some output.")

        # Must not raise
        await hook.after_turn(ctx, result)

    # ------------------------------------------------------------------
    # 17. Skip when history already has inbox message from self (no runtime_mgr)
    # ------------------------------------------------------------------

    async def test_skips_when_history_has_inbox_message_from_self__no_runtime_mgr(self):
        """RuntimeContextManager is None but history already contains an inbox
        message sent by this subagent → bus.send is NOT called.

        This reproduces the duplicate-send bug: when RuntimeContext is unavailable,
        the hook should fall back to checking history for evidence that the agent
        already communicated via send_to_agent.
        """
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        # No runtime_mgr → rc will be None
        history_entries = [
            {
                "role": "agent",
                "source_agent": "office-expert",
                "content": "<agent_message>Already sent</agent_message>",
                "meta_inbox": True,
                "meta_source": "office-expert",
                "meta_target_agent": "main",
            }
        ]
        ctx = self._make_ctx(history_entries, runtime_mgr=None)
        result = AgentResult(content="Task completed successfully.")

        await hook.after_turn(ctx, result)
        bus.send.assert_not_awaited()

    # ------------------------------------------------------------------
    # 18. Skip when history already has inbox message from self (empty tool_calls)
    # ------------------------------------------------------------------

    async def test_skips_when_history_has_inbox_message_from_self__empty_tool_calls(self):
        """RuntimeContext exists but get_tool_calls() is empty (e.g. hook missed
        recording), and history already contains an inbox message from this
        subagent → bus.send is NOT called.
        """
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        mgr = RuntimeContextManager()
        history_entries = [
            {
                "role": "agent",
                "source_agent": "office-expert",
                "content": "<agent_message>Already sent</agent_message>",
                "meta_inbox": True,
                "meta_source": "office-expert",
                "meta_target_agent": "main",
            }
        ]
        ctx = self._make_ctx(history_entries, runtime_mgr=mgr)
        result = AgentResult(content="Task completed successfully.")

        # RuntimeContext is empty (no tool calls recorded)
        await hook.after_turn(ctx, result)
        bus.send.assert_not_awaited()

    # ------------------------------------------------------------------
    # 19. Still forwards when inbox message is from a DIFFERENT agent
    # ------------------------------------------------------------------

    async def test_auto_forwards_when_history_has_inbox_message_from_other_agent(self):
        """History has an inbox message but from a different agent → bus.send IS
        called (this subagent has not yet communicated)."""
        bus = self._make_bus()
        hook = SubagentAutoSendHook(agent_bus=bus, self_name="office-expert", parent_name="main")
        history_entries = [
            {
                "role": "agent",
                "source_agent": "query_12306",
                "content": "<agent_message>12306 result</agent_message>",
                "meta_inbox": True,
                "meta_source": "query-12306",
                "meta_target_agent": "main",
            }
        ]
        ctx = self._make_ctx(history_entries, runtime_mgr=None)
        result = AgentResult(content="Task completed successfully.")

        await hook.after_turn(ctx, result)
        bus.send.assert_awaited_once()


# ── MaxIterationNotifyHook + SubagentAutoSendHook non-overlap tests ──


class TestMaxIterationAndAutoSendNonOverlap:
    """Prove that SubagentAutoSendHook and MaxIterationNotifyHook don't duplicate.

    Semantics:
      - normal stop, no send_to_agent → SubagentAutoSendHook auto-forwards
      - max_iterations stop              → MaxIterationNotifyHook notifies, SubagentAutoSendHook stays silent
    """

    @staticmethod
    def _make_ctx(session_id="conv_001:sub", runtime_mgr=None):
        from unittest.mock import MagicMock

        history = ListMessageHistory([])
        identity = TurnIdentity(agent_id="sub", session_id=session_id, turn_id="t1")
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

    def _make_auto_send_hook(self, bus, name="sub"):
        return SubagentAutoSendHook(agent_bus=bus, self_name=name, parent_name="main")

    # ── MaxIterationNotifyHook tests ──

    @staticmethod
    def _make_notify_svc():
        svc = AsyncMock()
        svc.notify = AsyncMock()
        return svc

    async def test_maxiter_notify_fires_when_max_iterations(self):
        """MaxIterationNotifyHook sends notification when stop_reason is max_iterations."""
        from framework.hook.notification import MaxIterationNotifyHook

        svc = self._make_notify_svc()
        hook = MaxIterationNotifyHook(notification_service=svc)
        ctx = self._make_ctx()
        result = AgentResult(content="Ran out.", stop_reason="max_iterations")

        await hook.after_turn(ctx, result)

        svc.notify.assert_awaited_once()

    async def test_maxiter_notify_silent_when_normal_stop(self):
        """MaxIterationNotifyHook does NOT notify when stop_reason is normal."""
        from framework.hook.notification import MaxIterationNotifyHook

        svc = self._make_notify_svc()
        hook = MaxIterationNotifyHook(notification_service=svc)
        ctx = self._make_ctx()
        result = AgentResult(content="Done.", stop_reason="completed")

        await hook.after_turn(ctx, result)

        svc.notify.assert_not_awaited()

    async def test_maxiter_notify_noop_when_svc_is_none(self):
        """Default constructor → after_turn returns without raising."""
        from framework.hook.notification import MaxIterationNotifyHook

        hook = MaxIterationNotifyHook()
        ctx = self._make_ctx()
        result = AgentResult(content="Done.", stop_reason="max_iterations")

        # Must not raise
        await hook.after_turn(ctx, result)

    # ── Non-overlap at max_iterations ──

    async def test_at_maxiter_auto_send_forwards_when_no_send_to_agent(self):
        """max_iterations + no send_to_agent → both hooks fire.
        SubagentAutoSendHook forwards partial output to parent
        (subagent may have useful work to report even at step limit)."""
        from unittest.mock import MagicMock
        from framework.hook.notification import MaxIterationNotifyHook

        bus = MagicMock()
        bus.send = AsyncMock()
        notify_svc = self._make_notify_svc()

        auto_send = self._make_auto_send_hook(bus)
        maxiter_hook = MaxIterationNotifyHook(notification_service=notify_svc)
        ctx = self._make_ctx()
        result = AgentResult(content="Out of steps.", stop_reason="max_iterations")

        await maxiter_hook.after_turn(ctx, result)
        await auto_send.after_turn(ctx, result)

        notify_svc.notify.assert_awaited_once()
        bus.send.assert_awaited_once()

    # ── Non-overlap at normal stop ──

    async def test_at_normal_stop_only_auto_send_fires_not_maxiter_notify(self):
        """Normal stop → SubagentAutoSendHook auto-forwards, MaxIterationNotifyHook is silent."""
        from unittest.mock import MagicMock
        from framework.hook.notification import MaxIterationNotifyHook

        bus = MagicMock()
        bus.send = AsyncMock()
        notify_svc = self._make_notify_svc()

        auto_send = self._make_auto_send_hook(bus)
        maxiter_hook = MaxIterationNotifyHook(notification_service=notify_svc)
        ctx = self._make_ctx()
        result = AgentResult(content="Task done.", stop_reason="completed")

        await auto_send.after_turn(ctx, result)
        await maxiter_hook.after_turn(ctx, result)

        bus.send.assert_awaited_once()
        notify_svc.notify.assert_not_awaited()
