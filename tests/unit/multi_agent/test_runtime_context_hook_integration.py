"""End-to-end tests for RuntimeContextHook + SubagentAutoSendHook collaboration.

Verifies:
- RuntimeContextHook auto-injection in AgentPipeline / AgentSession
- Correct hook ordering (RuntimeContextHook first)
- SubagentAutoSendHook detects send_to_agent via RuntimeContext
- Multiple hooks do not conflict
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from framework.core.agent import AgentContext
from framework.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from framework.runtime.models import TurnIdentity, TurnStateBase
from framework.runtime.services import AgentRuntime, AgentRuntimeServices
from framework.core.emitter import AgentResult, ContentEmitter
from framework.hook import Hook, HookErrorPolicy, HookSpec, HookRunner
from framework.hook.abc import AfterToolExecutionHook, AfterTurnHook, BeforeTurnHook
from framework.hook.builtin import RuntimeContextHook
from framework.core.runtime_context import RuntimeContextManager


def _make_runtime(hook_runner=None, runtime_mgr=None):
    """Build a minimal AgentRuntime with typed state for test contexts."""
    identity = TurnIdentity(agent_id="test", session_id="test", turn_id="t1")
    state = TurnStateBase(
        identity=identity, agent_kind=AgentKind.REACT, phase=TurnPhase.RUNNING,
    )
    services = AgentRuntimeServices(
        hooks=hook_runner,
        runtime_context_manager=runtime_mgr,
    )
    return AgentRuntime(services=services, state=state), identity
from framework.core.tool_manager import InMemoryToolManager
from framework.memory.history import ListMessageHistory
from framework.hook.builtin import SubagentAutoSendHook, RuntimeContextHook
from framework.pipeline.pipeline import AgentPipeline


class FakeAgent:
    """Agent that optionally calls send_to_agent via tool."""

    event_enum = None

    def __init__(self, tool_calls=None):
        self._tool_calls = tool_calls or []
        self.max_iterations = 5

    async def run(self, context: AgentContext, emitter: ContentEmitter) -> AgentResult:
        # Simulate ReActAgent._call_hooks(): prefer services.hooks, fallback to extension hooks
        hook_runner = None
        if context.runtime is not None and context.runtime.services is not None:
            hook_runner = context.runtime.services.hooks

        async def _call_hook_point(method_name: str, *args):
            if hook_runner is not None:
                from framework.hook import HookPoint, HookPayload
                payload_data = {}
                if method_name == "after_turn" and args:
                    payload_data = {"result": args[0]}
                elif method_name == "before_tool_execution" and args:
                    payload_data = {"tool_calls": args[0]}
                elif method_name == "after_tool_execution" and args:
                    payload_data = {"results": args[0]}
                await hook_runner.dispatch(
                    HookPoint(method_name), context,
                    HookPayload(data=payload_data), hook_timeout=10.0,
                )
                return
            for hook in _get_hooks_from_context(context):
                method = getattr(hook, method_name, None)
                if method is not None:
                    await method(context, *args)

        await _call_hook_point("before_turn")

        # Simulate tool execution if configured
        for tc in self._tool_calls:
            await _call_hook_point("before_tool_execution", [tc])
            tool_result = {"role": "tool", "tool_call_id": tc.call_id, "content": "ok"}
            await _call_hook_point("after_tool_execution", [tool_result])

        result = AgentResult(content="Task done.", stop_reason="final")
        await _call_hook_point("after_turn", result)
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


class TestRuntimeContextHookNoAutoInjection:
    """Verify RuntimeContextHook is NOT auto-injected by framework.

    It must be explicitly added to the hook_runner by business code
    (e.g. BotService._build_hook_runner).
    """

    def test_pipeline_does_not_auto_inject_runtime_context_hook(self):
        mgr = RuntimeContextManager()
        pipeline = AgentPipeline(
            agent=FakeAgent(),
            context_manager=MagicMock(),
            tool_manager=InMemoryToolManager(),
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            hooks=[MagicMock(spec=Hook)],
            runtime_context_manager=mgr,
        )
        assert not any(isinstance(h, RuntimeContextHook) for h in pipeline.hooks)

    def test_pipeline_no_injection_without_manager(self):
        pipeline = AgentPipeline(
            agent=FakeAgent(),
            context_manager=MagicMock(),
            tool_manager=InMemoryToolManager(),
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            hooks=[],
            runtime_context_manager=None,
        )
        assert not any(isinstance(h, RuntimeContextHook) for h in pipeline.hooks)

# ---------------------------------------------------------------------------
# 2. SubagentAutoSendHook + RuntimeContextHook collaboration
# ---------------------------------------------------------------------------


class TestHookCollaboration:
    """Verify SubagentAutoSendHook reads tool calls recorded by RuntimeContextHook."""

    def _make_bus(self):
        bus = MagicMock()
        bus.send = AsyncMock()
        return bus

    def _make_pipeline(self, agent, hooks, runtime_mgr=None):
        return AgentPipeline(
            agent=agent,
            context_manager=MagicMock(),
            tool_manager=InMemoryToolManager(),
            input_adapter=FakeInputAdapter(),
            output_adapter=FakeOutputAdapter(),
            hooks=hooks,
            runtime_context_manager=runtime_mgr,
        )

    async def test_subagent_auto_send_skips_when_runtime_context_records_send_to_agent(self):
        """Full flow: RuntimeContextHook records send_to_agent,
        SubagentAutoSendHook detects it and skips auto-forward."""
        bus = self._make_bus()
        runtime_mgr = RuntimeContextManager()

        subagent_hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="doc-expert", parent_name="main"
        )
        # RuntimeContextHook must be explicitly added to hook_runner
        # (framework no longer auto-injects it into pipeline.hooks).
        hook_runner = HookRunner()
        hook_runner.add(HookSpec(hook=RuntimeContextHook(), on_error=HookErrorPolicy.LOG))
        hook_runner.add(HookSpec(hook=subagent_hook, on_error=HookErrorPolicy.LOG))

        # Run the agent
        from framework.memory.history import ListMessageHistory
        runtime, identity = _make_runtime(hook_runner=hook_runner, runtime_mgr=runtime_mgr)
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="conv_001:main:doc-expert",
            runtime=runtime,
            identity=identity,
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
        hook_runner = HookRunner()
        hook_runner.add(HookSpec(hook=rch, on_error=HookErrorPolicy.LOG))
        runtime, identity = _make_runtime(hook_runner=hook_runner, runtime_mgr=runtime_mgr)
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="test_session",

            runtime=runtime,
            identity=identity,
        )

        # Resolve context
        await rch.before_turn(ctx)
        runtime_ctx = ctx.runtime._runtime_context
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
        """RuntimeContextHook + custom hook + SubagentAutoSendHook should coexist."""
        bus = self._make_bus()
        runtime_mgr = RuntimeContextManager()

        class _CallTrackingHook(BeforeTurnHook, AfterToolExecutionHook, AfterTurnHook):
            def __init__(self) -> None:
                self.before_turn_called = False
                self.after_turn_called = False

            @property
            def name(self) -> str:
                return "call_tracking"

            async def before_turn(self, ctx) -> None:
                self.before_turn_called = True

            async def after_tool_execution(self, ctx, results) -> None:
                pass

            async def after_turn(self, ctx, result) -> None:
                self.after_turn_called = True

        custom_hook = _CallTrackingHook()

        subagent_hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="doc-expert", parent_name="main"
        )

        # Explicitly build hook_runner (mirrors BotService._build_hook_runner)
        hook_runner = HookRunner()
        hook_runner.add(HookSpec(hook=RuntimeContextHook(), on_error=HookErrorPolicy.LOG))
        hook_runner.add(HookSpec(hook=custom_hook, on_error=HookErrorPolicy.LOG))
        hook_runner.add(HookSpec(hook=subagent_hook, on_error=HookErrorPolicy.LOG))

        from framework.memory.history import ListMessageHistory
        runtime, identity = _make_runtime(hook_runner=hook_runner, runtime_mgr=runtime_mgr)
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="conv_001:main:doc-expert",

            runtime=runtime,
            identity=identity,
        )

        await FakeAgent().run(ctx, MagicMock(spec=ContentEmitter))

        # All hooks should have been invoked
        assert custom_hook.before_turn_called
        assert custom_hook.after_turn_called

        # SubagentAutoSendHook should auto-forward (no tool calls)
        bus.send.assert_awaited_once()

    async def test_runtime_context_hook_must_be_in_hook_runner_for_subagent_agents(self):
        """Regression: RuntimeContextHook must be in hook_runner (not just
        pipeline.hooks) for SubagentAutoSendHook to detect communication tool calls.
        ReActAgent._call_hooks() prefers hook_runner and never falls back to
        hooks list. Business code must explicitly inject RuntimeContextHook
        into hook_runner (e.g. BotService._build_hook_runner).
        """
        bus = self._make_bus()
        runtime_mgr = RuntimeContextManager()

        subagent_hook = SubagentAutoSendHook(
            agent_bus=bus, self_name="doc-expert", parent_name="main"
        )

        # Simulate the bug: hook_runner lacks RuntimeContextHook.
        # Framework no longer auto-injects it anywhere.
        hook_runner = HookRunner()
        hook_runner.add(HookSpec(hook=subagent_hook, on_error=HookErrorPolicy.LOG))

        from framework.memory.history import ListMessageHistory
        runtime, identity = _make_runtime(hook_runner=hook_runner, runtime_mgr=runtime_mgr)
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="conv_001:main:doc-expert",

            runtime=runtime,
            identity=identity,
        )

        # Without the fix: hook_runner has no RuntimeContextHook �?
        # SubagentAutoSendHook sees empty tool_calls �?auto-forwards �?duplicate.
        await FakeAgent(tool_calls=[
            FakeToolCall("send_to_agent", "tc_1", {"target_agent": "main"})
        ]).run(ctx, MagicMock(spec=ContentEmitter))
        bus.send.assert_awaited_once()  # BUG: forwarded even though tool sent msg

        # Apply the fix (mirrors builders.py _initialize_additional_subagents)
        has_runtime_ctx = any(
            isinstance(spec.hook, RuntimeContextHook)
            for spec in hook_runner.hook_specs
        )
        assert not has_runtime_ctx  # confirms the bug scenario
        hook_runner.add(HookSpec(hook=RuntimeContextHook(), on_error=HookErrorPolicy.LOG))

        # Reset bus and re-run
        bus.send.reset_mock()
        ctx2 = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session_id="conv_001:main:doc-expert",

            runtime=runtime,
            identity=identity,
        )
        await FakeAgent(tool_calls=[
            FakeToolCall("send_to_agent", "tc_1", {"target_agent": "main"})
        ]).run(ctx2, MagicMock(spec=ContentEmitter))
        bus.send.assert_not_awaited()  # FIX: correctly skipped


