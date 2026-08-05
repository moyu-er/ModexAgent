"""Unit tests for ``ReactGraphRuntime`` — verify AOP service routing.

Per ADR-0033 D5 + D13 Stage 1: ``ReactGraphRuntime`` bridges
``modex_graph``'s ``GraphRuntime`` ABC to ``modex_agent``'s AOP services.
These tests construct the runtime with mock services and verify that each
method routes to the correct underlying service with the correct
arguments.

Key verification points:
- ``dispatch_hook`` maps ``ReActHookPoint`` → ``HookPoint`` and wraps
  ``data: dict`` into ``HookPayload``.
- ``around(ITERATION)`` constructs ``IterationContext`` and calls
  ``around_iteration``.
- ``around(TOOL_CALL/LLM_STREAM/LLM_CALL)`` are pass-throughs.
- ``apply_governance`` delegates to ``ContextGovernance.apply``.
- ``drain_control`` calls ``drain_control_channel`` helper.
- ``capture_snapshot`` calls ``SnapshotPolicy.capture`` + ``TurnStateStore.save_turn``.
- ``emit`` maps event_type string to ``ReActEvent`` enum and calls ``emitter.emit``.
- ``before_node`` / ``after_node`` are no-ops.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.agents.react.constants import ReActHookPoint, ReActScope
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.hook.abc import HookPayload, HookPoint
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, SnapshotReason, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_graph import create_null_coordinator
from modex_graph.context import GraphContext
from modex_graph.runtime import GraphRuntime
from modex_graph.state import GraphState


def _make_agent_ctx(iteration: int = 3) -> AgentContext:
    state = ReActTurnState(
        identity=TurnIdentity(
            agent_id="test", session=SessionInfo.from_str("s1.test"), turn_id="t1"
        ),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        iteration=iteration,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("s1.test"),
        identity=state.identity,
        runtime=runtime,
    )


def _make_graph_ctx(agent_ctx: AgentContext, runtime: GraphRuntime) -> GraphContext[GraphState]:
    return GraphContext(
        state=GraphState(),
        runtime=runtime,
        user_data=agent_ctx,
        coordinator=create_null_coordinator(),
    )


class TestReactGraphRuntimeConstruction:
    def test_implements_graph_runtime(self) -> None:
        rt = ReactGraphRuntime()
        assert isinstance(rt, GraphRuntime)

    def test_no_services_is_no_op(self) -> None:
        rt = ReactGraphRuntime()
        assert rt._hook_runner is None
        assert rt._interceptor_chain is None
        assert rt._governance is None
        assert rt._control_channel is None
        assert rt._snapshot_policy is None
        assert rt._turn_state_store is None
        assert rt._emitter is None

    def test_hook_point_map_covers_all_react_hook_points(self) -> None:
        for hp in ReActHookPoint:
            assert hp in ReactGraphRuntime.HOOK_POINT_MAP, f"missing {hp}"
            assert ReactGraphRuntime.HOOK_POINT_MAP[hp] in HookPoint


class TestDispatchHook:
    async def test_dispatches_with_data(self) -> None:
        mock_runner = MagicMock()
        mock_runner.dispatch = AsyncMock(return_value=None)
        rt = ReactGraphRuntime(hook_runner=mock_runner)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        await rt.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx, data={"key": "value"})

        mock_runner.dispatch.assert_awaited_once()
        call_args = mock_runner.dispatch.call_args
        assert call_args.args[0] == HookPoint.BEFORE_ITERATION
        assert call_args.args[1] is agent_ctx
        payload: HookPayload = call_args.kwargs["payload"]
        assert payload.data == {"key": "value"}

    async def test_dispatches_without_data(self) -> None:
        mock_runner = MagicMock()
        mock_runner.dispatch = AsyncMock(return_value=None)
        rt = ReactGraphRuntime(hook_runner=mock_runner)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        await rt.dispatch_hook(ReActHookPoint.AFTER_LLM_RESPONSE, ctx)

        mock_runner.dispatch.assert_awaited_once()
        call_args = mock_runner.dispatch.call_args
        assert call_args.args[0] == HookPoint.AFTER_LLM_RESPONSE
        assert call_args.kwargs["payload"] is None

    async def test_noop_when_hook_runner_none(self) -> None:
        rt = ReactGraphRuntime()
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)
        await rt.dispatch_hook(ReActHookPoint.BEFORE_ITERATION, ctx, data={"k": "v"})

    async def test_noop_for_unknown_hook_point(self) -> None:
        mock_runner = MagicMock()
        mock_runner.dispatch = AsyncMock(return_value=None)
        rt = ReactGraphRuntime(hook_runner=mock_runner)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        await rt.dispatch_hook("nonexistent_hook", ctx)

        mock_runner.dispatch.assert_not_awaited()

    async def test_all_hook_points_mapped(self) -> None:
        mock_runner = MagicMock()
        mock_runner.dispatch = AsyncMock(return_value=None)
        rt = ReactGraphRuntime(hook_runner=mock_runner)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        for hp in ReActHookPoint:
            mock_runner.dispatch.reset_mock()
            await rt.dispatch_hook(hp, ctx)
            mock_runner.dispatch.assert_awaited_once()
            assert mock_runner.dispatch.call_args.args[0] == ReactGraphRuntime.HOOK_POINT_MAP[hp]


class TestAround:
    async def test_iteration_calls_around_iteration(self) -> None:
        mock_chain = MagicMock()
        mock_chain.around_iteration = AsyncMock(return_value=None)
        rt = ReactGraphRuntime(interceptor_chain=mock_chain)
        agent_ctx = _make_agent_ctx(iteration=5)
        ctx = _make_graph_ctx(agent_ctx, rt)

        body_called = False

        async def body() -> None:
            nonlocal body_called
            body_called = True

        await rt.around(ReActScope.ITERATION, ctx, body)

        mock_chain.around_iteration.assert_awaited_once()
        call_args = mock_chain.around_iteration.call_args
        assert call_args.args[0] is agent_ctx
        ic = call_args.args[1]
        assert ic.iteration == 5
        assert ic.turn_id == "s1.test"

    async def test_iteration_passes_turn_state(self) -> None:
        mock_chain = MagicMock()
        mock_chain.around_iteration = AsyncMock(return_value=None)
        rt = ReactGraphRuntime(interceptor_chain=mock_chain)
        agent_ctx = _make_agent_ctx(iteration=2)
        ctx = _make_graph_ctx(agent_ctx, rt)

        async def body() -> None:
            pass

        await rt.around(ReActScope.ITERATION, ctx, body)

        ic = mock_chain.around_iteration.call_args.args[1]
        assert agent_ctx.runtime is not None
        assert ic.turn_state is agent_ctx.runtime.state

    async def test_tool_call_is_pass_through(self) -> None:
        mock_chain = MagicMock()
        mock_chain.around_iteration = AsyncMock(return_value=None)
        rt = ReactGraphRuntime(interceptor_chain=mock_chain)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        body_called = False

        async def body() -> str:
            nonlocal body_called
            body_called = True
            return "result"

        result = await rt.around(ReActScope.TOOL_CALL, ctx, body)
        assert body_called
        assert result == "result"
        mock_chain.around_iteration.assert_not_awaited()

    async def test_llm_stream_is_pass_through(self) -> None:
        mock_chain = MagicMock()
        rt = ReactGraphRuntime(interceptor_chain=mock_chain)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        async def body() -> str:
            return "streamed"

        result = await rt.around(ReActScope.LLM_STREAM, ctx, body)
        assert result == "streamed"

    async def test_llm_call_is_pass_through(self) -> None:
        mock_chain = MagicMock()
        rt = ReactGraphRuntime(interceptor_chain=mock_chain)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        async def body() -> str:
            return "called"

        result = await rt.around(ReActScope.LLM_CALL, ctx, body)
        assert result == "called"

    async def test_no_interceptor_chain_is_pass_through(self) -> None:
        rt = ReactGraphRuntime()
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        async def body() -> str:
            return "passthrough"

        result = await rt.around(ReActScope.ITERATION, ctx, body)
        assert result == "passthrough"


class TestApplyGovernance:
    async def test_delegates_to_governance(self) -> None:
        mock_gov = MagicMock()
        mock_gov.apply = AsyncMock(return_value=[{"role": "system", "content": "filtered"}])
        rt = ReactGraphRuntime(governance=mock_gov)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        original = [{"role": "user", "content": "hi"}]
        result = await rt.apply_governance(original, ctx)

        mock_gov.apply.assert_awaited_once_with(original, agent_ctx)
        assert result == [{"role": "system", "content": "filtered"}]

    async def test_returns_messages_unchanged_when_no_governance(self) -> None:
        rt = ReactGraphRuntime()
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        messages = [{"role": "user", "content": "hi"}]
        result = await rt.apply_governance(messages, ctx)
        assert result is messages


class TestDrainControl:
    async def test_calls_drain_control_channel(self) -> None:
        mock_channel = MagicMock()
        rt = ReactGraphRuntime(control_channel=mock_channel)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        with pytest.MonkeyPatch().context() as m:
            mock_drain = AsyncMock(return_value=False)
            m.setattr(
                "modex_agent.hook.builtin.control_drain.drain_control_channel",
                mock_drain,
            )
            await rt.drain_control(ctx)

        mock_drain.assert_awaited_once()
        call_args = mock_drain.call_args
        assert call_args.args[0] is mock_channel
        assert call_args.args[1] is agent_ctx

    async def test_noop_when_no_control_channel(self) -> None:
        rt = ReactGraphRuntime()
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)
        await rt.drain_control(ctx)


class TestCaptureSnapshot:
    async def test_captures_and_saves(self) -> None:
        mock_snapshot = MagicMock()
        mock_policy = MagicMock()
        mock_policy.capture = MagicMock(return_value=mock_snapshot)
        mock_store = MagicMock()
        mock_store.save_turn = AsyncMock()
        rt = ReactGraphRuntime(
            snapshot_policy=mock_policy,
            turn_state_store=mock_store,
        )
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        await rt.capture_snapshot(ctx, "tool_approval_required")

        assert agent_ctx.runtime is not None
        mock_policy.capture.assert_called_once_with(
            agent_ctx.runtime.state,
            SnapshotReason.TOOL_APPROVAL_REQUIRED,
        )
        mock_store.save_turn.assert_awaited_once_with(mock_snapshot)

    async def test_noop_when_no_snapshot_policy(self) -> None:
        mock_store = MagicMock()
        mock_store.save_turn = AsyncMock()
        rt = ReactGraphRuntime(turn_state_store=mock_store)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)
        await rt.capture_snapshot(ctx, "iteration")
        mock_store.save_turn.assert_not_awaited()

    async def test_noop_when_no_turn_state_store(self) -> None:
        mock_policy = MagicMock()
        mock_policy.capture = MagicMock(return_value=MagicMock())
        rt = ReactGraphRuntime(snapshot_policy=mock_policy)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)
        await rt.capture_snapshot(ctx, "iteration")
        mock_policy.capture.assert_not_called()


class TestEmit:
    async def test_emits_known_event(self) -> None:
        from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent

        mock_emitter = MagicMock()
        mock_emitter.emit = AsyncMock()
        rt = ReactGraphRuntime(emitter=mock_emitter)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        payload = {"content": "hello"}
        await rt.emit(GraphReActEvent.MODEL_OUTPUT, payload, ctx)

        mock_emitter.emit.assert_awaited_once_with(ReActEvent.MODEL_OUTPUT, payload)

    async def test_emits_all_known_events(self) -> None:
        from modex_agent.agents.react.constants import ReActEvent as GraphReActEvent

        mock_emitter = MagicMock()
        mock_emitter.emit = AsyncMock()
        rt = ReactGraphRuntime(emitter=mock_emitter)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        for graph_ev in GraphReActEvent:
            mock_emitter.emit.reset_mock()
            await rt.emit(graph_ev, None, ctx)
            mock_emitter.emit.assert_awaited_once()
            emitted_event = mock_emitter.emit.call_args.args[0]
            assert isinstance(emitted_event, ReActEvent)
            assert emitted_event.value == graph_ev.value

    async def test_noop_when_no_emitter(self) -> None:
        rt = ReactGraphRuntime()
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)
        await rt.emit("model_output", {"data": 1}, ctx)

    async def test_skips_unknown_event_type(self) -> None:
        mock_emitter = MagicMock()
        mock_emitter.emit = AsyncMock()
        rt = ReactGraphRuntime(emitter=mock_emitter)
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)

        await rt.emit("nonexistent_event", None, ctx)
        mock_emitter.emit.assert_not_awaited()


class TestEngineAutoMethods:
    async def test_before_node_is_noop(self) -> None:
        rt = ReactGraphRuntime()
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)
        await rt.before_node(ctx, "llm")

    async def test_after_node_is_noop(self) -> None:
        rt = ReactGraphRuntime()
        agent_ctx = _make_agent_ctx()
        ctx = _make_graph_ctx(agent_ctx, rt)
        await rt.after_node(ctx, "llm")
