"""End-to-end tests for RuntimeContextHook + SubagentAutoSendHook collaboration.

Verifies:
- RuntimeContextHook is NOT auto-injected by framework
- SubagentAutoSendHook (FinallyGraphHook) always fires on finally_graph
- RuntimeContextHook records tool calls correctly
- Multiple hooks do not conflict
"""

from unittest.mock import AsyncMock, MagicMock

from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, ContentEmitter, StopReason
from modex_agent.core.session_id import SessionInfo
from modex_agent.hook import Hook, HookErrorPolicy, HookRunner, HookSpec
from modex_agent.hook.abc import AfterToolExecutionHook, AfterTurnHook, BeforeTurnHook
from modex_agent.hook.builtin import SubagentAutoSendHook
from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
from modex_agent.runtime.context import RuntimeContextManager
from modex_agent.runtime.enums import AgentKind, TurnPhase
from modex_agent.runtime.hooks import RuntimeContextHook
from modex_agent.runtime.models import TurnIdentity, TurnStateBase
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


def _mock_tree(bus: object) -> SessionTreeManager:
    tree: SessionTreeManager = MagicMock(spec=SessionTreeManager)

    async def _deliver(sid: str, env: object) -> None:
        await bus.send(sid, env)  # type: ignore[attr-defined]

    tree.deliver = _deliver
    return tree



def _make_runtime(hook_runner=None, runtime_mgr=None):
    """Build a minimal AgentRuntime with typed state for test contexts."""
    identity = TurnIdentity(agent_id="test", session=SessionInfo.from_str("test"), turn_id="t1")
    state = TurnStateBase(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
    )
    services = AgentRuntimeServices(
        hooks=hook_runner,
        runtime_context_manager=runtime_mgr,
    )
    return AgentRuntime(services=services, state=state), identity


from modex_agent.memory.history import ListMessageHistory
from modex_agent.tools.manager import InMemoryToolManager
from tests.unit.pipeline._helpers import _make_react_pipeline


class FakeAgent:
    """Agent that optionally simulates tool calls and dispatches hooks."""

    event_enum = None

    def __init__(self, tool_calls=None):
        self._tool_calls = tool_calls or []
        self.max_iterations = 5

    async def run(self, context: AgentContext, emitter: ContentEmitter) -> AgentResult:
        hook_runner = None
        if context.runtime is not None and context.runtime.services is not None:
            hook_runner = context.runtime.services.hooks

        async def _call_hook_point(method_name: str, *args):
            if hook_runner is None:
                return

            from modex_agent.hook import HookPayload, HookPoint

            payload_data = {}
            if method_name in ("after_turn", "finally_graph") and args:
                payload_data = {"result": args[0]}
            elif method_name == "before_tool_execution" and args:
                payload_data = {"tool_calls": args[0]}
            elif method_name == "after_tool_execution" and args:
                payload_data = {"results": args[0]}
            await hook_runner.dispatch(
                HookPoint(method_name),
                context,
                HookPayload(data=payload_data),
                hook_timeout=10.0,
            )

        await _call_hook_point("start_node_turn")
        await _call_hook_point("before_turn")

        # Simulate tool execution if configured
        for tc in self._tool_calls:
            await _call_hook_point("before_tool_execution", [tc])
            tool_result = {"role": "tool", "tool_call_id": tc.call_id, "content": "ok"}
            await _call_hook_point("after_tool_execution", [tool_result])

        result = AgentResult(content="Task done.", stop_reason=StopReason.COMPLETED)
        await _call_hook_point("after_turn", result)
        await _call_hook_point("finally_graph", result)
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
        pipeline = _make_react_pipeline(
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
        pipeline = _make_react_pipeline(
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
# 2. SubagentAutoSendHook (FinallyGraphHook) + RuntimeContextHook collaboration
# ---------------------------------------------------------------------------


class TestHookCollaboration:
    """Verify SubagentAutoSendHook always fires via finally_graph."""

    def _make_bus(self):
        bus = MagicMock()
        bus.send = AsyncMock()
        return bus

    async def test_subagent_auto_send_always_fires_on_finally_graph(self):
        """SubagentAutoSendHook always fires on finally_graph,
        regardless of whether send_to_agent was called."""
        bus = self._make_bus()

        subagent_hook = SubagentAutoSendHook(
        tree=_mock_tree(bus),
        )
        hook_runner = HookRunner()
        hook_runner.add(HookSpec(hook=RuntimeContextHook(), on_error=HookErrorPolicy.LOG))
        hook_runner.add(HookSpec(hook=subagent_hook, on_error=HookErrorPolicy.LOG))


        runtime, identity = _make_runtime(
            hook_runner=hook_runner, runtime_mgr=RuntimeContextManager()
        )
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id="inv-123.agent",
                agent_name="agent",
                parent_session_id="parent.main",
            ),
            runtime=runtime,
            identity=identity,
        )
        await FakeAgent(tool_calls=[FakeToolCall("search", "tc_1", {"q": "foo"})]).run(
            ctx, MagicMock(spec=ContentEmitter)
        )

        bus.send.assert_awaited_once()
        _inbox_key, envelope = bus.send.call_args.args
        assert envelope.invocation_id == "inv-123"

    async def test_subagent_auto_send_fires_even_with_send_to_agent(self):
        """SubagentAutoSendHook always fires — no skip logic.
        The hook is the sole notification path for subagents."""
        bus = self._make_bus()

        subagent_hook = SubagentAutoSendHook(
        tree=_mock_tree(bus),
        )
        hook_runner = HookRunner()
        hook_runner.add(HookSpec(hook=RuntimeContextHook(), on_error=HookErrorPolicy.LOG))
        hook_runner.add(HookSpec(hook=subagent_hook, on_error=HookErrorPolicy.LOG))


        runtime, identity = _make_runtime(
            hook_runner=hook_runner, runtime_mgr=RuntimeContextManager()
        )
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id="inv-123.agent",
                agent_name="agent",
                parent_session_id="parent.main",
            ),
            runtime=runtime,
            identity=identity,
        )
        # Even though send_to_agent was called, the hook still fires
        await FakeAgent(
            tool_calls=[FakeToolCall("send_to_agent", "tc_1", {"target_agent": "main"})]
        ).run(ctx, MagicMock(spec=ContentEmitter))

        bus.send.assert_awaited_once()
        _inbox_key, envelope = bus.send.call_args.args
        assert envelope.invocation_id == "inv-123"

    async def test_runtime_context_hook_records_tool_calls_correctly(self):
        """RuntimeContextHook.before_tool_execution + after_tool_execution
        records complete ToolCallRecord into the RuntimeContext."""
        runtime_mgr = RuntimeContextManager()
        rch = RuntimeContextHook()


        hook_runner = HookRunner()
        hook_runner.add(HookSpec(hook=rch, on_error=HookErrorPolicy.LOG))
        runtime, identity = _make_runtime(hook_runner=hook_runner, runtime_mgr=runtime_mgr)
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo.from_str("test.agent"),
            runtime=runtime,
            identity=identity,
        )

        # Resolve context
        await rch.start_node_turn(ctx)
        runtime_ctx = ctx.runtime.runtime_context
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
        tree=_mock_tree(bus),
        )

        # Explicitly build hook_runner (mirrors BotService._build_hook_runner)
        hook_runner = HookRunner()
        hook_runner.add(HookSpec(hook=RuntimeContextHook(), on_error=HookErrorPolicy.LOG))
        hook_runner.add(HookSpec(hook=custom_hook, on_error=HookErrorPolicy.LOG))
        hook_runner.add(HookSpec(hook=subagent_hook, on_error=HookErrorPolicy.LOG))


        runtime, identity = _make_runtime(hook_runner=hook_runner, runtime_mgr=runtime_mgr)
        ctx = AgentContext(
            system_prompt="",
            history=ListMessageHistory([]),
            tool_manager=InMemoryToolManager(),
            session=SessionInfo(
                session_id="inv-123.agent",
                agent_name="agent",
                parent_session_id="parent.main",
            ),
            runtime=runtime,
            identity=identity,
        )

        await FakeAgent().run(ctx, MagicMock(spec=ContentEmitter))

        # All hooks should have been invoked
        assert custom_hook.before_turn_called
        assert custom_hook.after_turn_called

        # SubagentAutoSendHook always fires (FinallyGraphHook)
        bus.send.assert_awaited_once()
        _inbox_key, envelope = bus.send.call_args.args
        assert envelope.invocation_id == "inv-123"
