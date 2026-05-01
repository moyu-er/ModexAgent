"""End-to-end tests for RuntimeContextHook + PeerAutoSendHook collaboration.

Verifies:
- RuntimeContextHook auto-injection in AgentPipeline / AgentSession
- Correct hook ordering (RuntimeContextHook first)
- PeerAutoSendHook detects send_message_async via RuntimeContext
- Multiple hooks do not conflict
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.agent import AgentContext
from framework.core.context import InMemoryContextManager
from framework.core.context_extensions import ExtensionKey
from framework.core.emitter import AgentResult, ContentEmitter
from framework.hook import Hook
from framework.hook.builtin import RuntimeContextHook
from framework.core.runtime_context import RuntimeContextManager
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory
from framework.hook.builtin import PeerAutoSendHook, RuntimeContextHook
from framework.pipeline.pipeline import AgentPipeline
from framework.session.agent_session import AgentSession


class FakeAgent:
    """Agent that optionally calls send_message_async via tool."""

    event_enum = None

    def __init__(self, tool_calls=None):
        self._tool_calls = tool_calls or []
        self.max_iterations = 5

    async def run(self, context: AgentContext, emitter: ContentEmitter) -> AgentResult:
        # Simulate hook lifecycle manually (as ReActAgent would)
        for hook in context.extensions.get(ExtensionKey.HOOKS, []):
            if hasattr(hook, "before_turn"):
                await hook.before_turn(context)

        # Simulate tool execution if configured
        for tc in self._tool_calls:
            for hook in context.extensions.get(ExtensionKey.HOOKS, []):
                if hasattr(hook, "before_tool_execution"):
                    await hook.before_tool_execution(context, [tc])

            # Simulate tool result
            tool_result = {"role": "tool", "tool_call_id": tc.call_id, "content": "ok"}

            for hook in context.extensions.get(ExtensionKey.HOOKS, []):
                if hasattr(hook, "after_tool_execution"):
                    await hook.after_tool_execution(context, [tool_result])

        result = AgentResult(content="Task done.", stop_reason="final")

        for hook in context.extensions.get(ExtensionKey.HOOKS, []):
            if hasattr(hook, "after_turn"):
                await hook.after_turn(context, result)

        return result


class FakeToolCall:
    def __init__(self, tool_name, call_id, arguments=None):
        self.tool_name = tool_name
        self.call_id = call_id
        self.arguments = arguments or {}


class FakeInputAdapter:
    async def start(self):
        pass

    async def get_next_message(self):
        return None


class FakeOutputAdapter:
    async def send(self, msg):
        pass


# ---------------------------------------------------------------------------
# 1. Auto-injection ordering
# ---------------------------------------------------------------------------


class TestRuntimeContextHookAutoInjection:
    """Verify RuntimeContextHook is auto-injected and placed first."""

    def test_pipeline_injects_runtime_context_hook_at_front(self):
        mgr = RuntimeContextManager()
        pipeline = AgentPipeline(
            agent=FakeAgent(),
            context_manager=InMemoryContextManager(),
            tool_manager=InMemoryToolManager(),
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            hooks=[MagicMock(spec=Hook)],
            runtime_context_manager=mgr,
        )
        assert len(pipeline.hooks) >= 1
        assert isinstance(pipeline.hooks[0], RuntimeContextHook)

    def test_pipeline_skips_duplicate_injection(self):
        mgr = RuntimeContextManager()
        existing = RuntimeContextHook()
        pipeline = AgentPipeline(
            agent=FakeAgent(),
            context_manager=InMemoryContextManager(),
            tool_manager=InMemoryToolManager(),
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            hooks=[existing],
            runtime_context_manager=mgr,
        )
        rch_count = sum(1 for h in pipeline.hooks if isinstance(h, RuntimeContextHook))
        assert rch_count == 1

    def test_pipeline_no_injection_without_manager(self):
        pipeline = AgentPipeline(
            agent=FakeAgent(),
            context_manager=InMemoryContextManager(),
            tool_manager=InMemoryToolManager(),
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            hooks=[],
            runtime_context_manager=None,
        )
        assert not any(isinstance(h, RuntimeContextHook) for h in pipeline.hooks)

    def test_session_injects_runtime_context_hook_at_front(self):
        mgr = RuntimeContextManager()
        session = AgentSession(
            agent=FakeAgent(),
            context_manager=InMemoryContextManager(),
            tool_manager=InMemoryToolManager(),
            hooks=[MagicMock(spec=Hook)],
            runtime_context_manager=mgr,
        )
        assert len(session._hooks) >= 1
        assert isinstance(session._hooks[0], RuntimeContextHook)


# ---------------------------------------------------------------------------
# 2. PeerAutoSendHook + RuntimeContextHook collaboration
# ---------------------------------------------------------------------------


class TestHookCollaboration:
    """Verify PeerAutoSendHook reads tool calls recorded by RuntimeContextHook."""

    def _make_bus(self):
        bus = MagicMock()
        bus.send = AsyncMock()
        return bus

    def _make_pipeline(self, agent, hooks, runtime_mgr=None):
        return AgentPipeline(
            agent=agent,
            context_manager=InMemoryContextManager(),
            tool_manager=InMemoryToolManager(),
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            hooks=hooks,
            runtime_context_manager=runtime_mgr,
        )

    async def test_peer_auto_send_skips_when_runtime_context_records_send_message_async(self):
        """Full flow: RuntimeContextHook records send_message_async,
        PeerAutoSendHook detects it and skips auto-forward."""
        bus = self._make_bus()
        runtime_mgr = RuntimeContextManager()

        peer_hook = PeerAutoSendHook(
            agent_bus=bus, self_name="doc-expert", parent_name="main"
        )
        pipeline = self._make_pipeline(
            agent=FakeAgent(tool_calls=[
                FakeToolCall("send_message_async", "tc_1", {"target_agent": "main"})
            ]),
            hooks=[peer_hook],
            runtime_mgr=runtime_mgr,
        )

        # Manually simulate what ReActAgent does (hooks already called in FakeAgent.run)
        # Just verify the hook ordering: RuntimeContextHook should be first
        assert isinstance(pipeline.hooks[0], RuntimeContextHook)

        # Run the agent
        from framework.memory.history import ListMessageHistory
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="conv_001:main:doc-expert",
            metadata={"session_id": "conv_001:main:doc-expert"},
            extensions={
                ExtensionKey.HOOKS: pipeline.hooks,
                ExtensionKey.RUNTIME_CTX_MGR: runtime_mgr,
            },
        )
        await FakeAgent(tool_calls=[
            FakeToolCall("send_message_async", "tc_1", {"target_agent": "main"})
        ]).run(ctx, MagicMock(spec=ContentEmitter))

        # PeerAutoSendHook should have skipped bus.send
        bus.send.assert_not_awaited()

    async def test_peer_auto_send_forwards_when_no_comm_tool_in_runtime_context(self):
        """Full flow: no send_message_async recorded,
        PeerAutoSendHook auto-forwards content."""
        bus = self._make_bus()
        runtime_mgr = RuntimeContextManager()

        peer_hook = PeerAutoSendHook(
            agent_bus=bus, self_name="doc-expert", parent_name="main"
        )
        pipeline = self._make_pipeline(
            agent=FakeAgent(tool_calls=[
                FakeToolCall("search", "tc_1", {"q": "foo"})
            ]),
            hooks=[peer_hook],
            runtime_mgr=runtime_mgr,
        )

        assert isinstance(pipeline.hooks[0], RuntimeContextHook)

        from framework.memory.history import ListMessageHistory
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="conv_001:main:doc-expert",
            metadata={"session_id": "conv_001:main:doc-expert"},
            extensions={
                ExtensionKey.HOOKS: pipeline.hooks,
                ExtensionKey.RUNTIME_CTX_MGR: runtime_mgr,
            },
        )
        await FakeAgent(tool_calls=[
            FakeToolCall("search", "tc_1", {"q": "foo"})
        ]).run(ctx, MagicMock(spec=ContentEmitter))

        bus.send.assert_awaited_once()

    async def test_runtime_context_hook_records_tool_calls_correctly(self):
        """RuntimeContextHook.before_tool_execution + after_tool_execution
        records complete ToolCallRecord into the RuntimeContext."""
        runtime_mgr = RuntimeContextManager()
        rch = RuntimeContextHook()

        from framework.memory.history import ListMessageHistory
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="test_session",
            metadata={},
            extensions={
                ExtensionKey.HOOKS: [rch],
                ExtensionKey.RUNTIME_CTX_MGR: runtime_mgr,
            },
        )

        # Resolve context
        await rch.before_turn(ctx)
        runtime_ctx = ctx.extensions.get(ExtensionKey.RUNTIME_CTX)
        assert runtime_ctx is not None

        # Simulate tool execution
        tool_call = FakeToolCall("weather", "tc_1", {"city": "Beijing"})
        await rch.before_tool_execution(ctx, [tool_call])

        result_msg = {"role": "tool", "tool_call_id": "tc_1", "content": "Sunny 25C"}
        await rch.after_tool_execution(ctx, [result_msg])

        calls = await runtime_ctx.get_tool_calls()
        assert len(calls) == 1
        assert calls[0].tool_name == "weather"
        assert calls[0].arguments == {"city": "Beijing"}
        assert calls[0].result == "Sunny 25C"

    async def test_multiple_hooks_no_conflict(self):
        """RuntimeContextHook + custom hook + PeerAutoSendHook should coexist."""
        bus = self._make_bus()
        runtime_mgr = RuntimeContextManager()

        custom_hook = MagicMock(spec=Hook)
        custom_hook.before_turn = AsyncMock()
        custom_hook.after_tool_execution = AsyncMock()
        custom_hook.after_turn = AsyncMock()

        peer_hook = PeerAutoSendHook(
            agent_bus=bus, self_name="doc-expert", parent_name="main"
        )

        pipeline = self._make_pipeline(
            agent=FakeAgent(),
            hooks=[custom_hook, peer_hook],
            runtime_mgr=runtime_mgr,
        )

        # RuntimeContextHook should be first
        assert isinstance(pipeline.hooks[0], RuntimeContextHook)

        from framework.memory.history import ListMessageHistory
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="conv_001:main:doc-expert",
            metadata={"session_id": "conv_001:main:doc-expert"},
            extensions={
                ExtensionKey.HOOKS: pipeline.hooks,
                ExtensionKey.RUNTIME_CTX_MGR: runtime_mgr,
            },
        )

        await FakeAgent().run(ctx, MagicMock(spec=ContentEmitter))

        # All hooks should have been invoked
        custom_hook.before_turn.assert_awaited_once()
        custom_hook.after_turn.assert_awaited_once()

        # PeerAutoSendHook should auto-forward (no tool calls)
        bus.send.assert_awaited_once()
