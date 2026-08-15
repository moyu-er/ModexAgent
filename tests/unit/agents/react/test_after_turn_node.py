# ruff: noqa: ANN001, ANN401
"""Tests for AfterTurnNode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.nodes.after_turn import AfterTurnNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import ReActTurnState, get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ChatMessage
from modex_agent.core.types import MessageRole
from modex_agent.hook import AfterTurnHook, HookRunner, HookSpec
from modex_agent.runtime.enums import MessageDeltaSource, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import MessageDelta
from modex_agent.runtime.services import (
    AgentRuntime,
    require_runtime_state,
)


def _react_state(runtime: AgentRuntime) -> ReActTurnState:
    return require_runtime_state(runtime, ReActTurnState)


def _append_assistant(state: ReActTurnState, content: str) -> None:
    state.message_delta.append(
        MessageDelta(
            message=ChatMessage(role=MessageRole.ASSISTANT, content=content),
            source=MessageDeltaSource.ASSISTANT,
        )
    )


class _TrackingAfterTurnHook(AfterTurnHook):
    def __init__(self, node: AfterTurnNode) -> None:
        self._node = node
        self.result: AgentResult | None = None

    async def after_turn(self, ctx: AgentContext, result: AgentResult) -> None:
        state = get_react_state(ctx)
        assert state is not None
        assert state.result is result
        assert TurnCustomKey.CONTINUATION_REQUEST in state.custom
        self.result = result


class TestAfterTurnNodeResultConstruction:
    """AgentResult construction (also verifies state.result is written)."""

    async def test_cancelled_branch(self, make_runtime, make_graph_ctx) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        _react_state(runtime).phase = TurnPhase.CANCELLED
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert ctx.state.result is not None
        assert ctx.state.result.stop_reason == StopReason.TURN_CANCELLED
        assert ctx.state.result.content == "turn cancelled"

    async def test_error_branch(
        self, make_runtime, make_graph_ctx
    ) -> None:
        node = AfterTurnNode()
        node.node_id = ReActNode.AFTER.value  # type: ignore[attr-defined]
        runtime = make_runtime()
        ctx = make_graph_ctx(runtime=runtime)
        # LLM errors arrive as {"error": text} deliver payload to AFTER.
        ctx.coordinator.route_deliver(
            ReActNode.AFTER, {"error": "boom"}, ReActNode.LLM, 0
        )

        await node.run(ctx)

        assert ctx.state.result is not None
        assert ctx.state.result.stop_reason == StopReason.ERROR
        assert ctx.state.result.error == "boom"

    async def test_tool_error_phase_without_llm_response(
        self, make_runtime, make_graph_ctx
    ) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        state = _react_state(runtime)
        state.phase = TurnPhase.FAILED
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert ctx.state.result is not None
        assert ctx.state.result.stop_reason == StopReason.ERROR
        assert ctx.state.result.error == "tool execution error"

    async def test_normal_branch(
        self, make_runtime, make_graph_ctx
    ) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        state = _react_state(runtime)
        # Final answer read from state.message_delta, not state.llm_response.
        state.message_delta.append(
            MessageDelta(
                message=ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content="Hello!",
                    reasoning_content="thinking...",
                ),
                source=MessageDeltaSource.ASSISTANT,
            )
        )
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert ctx.state.result is not None
        assert ctx.state.result.content == "Hello!"
        assert ctx.state.result.reasoning == "thinking..."
        assert ctx.state.result.stop_reason == StopReason.COMPLETED

    async def test_max_iterations_branch(self, make_runtime, make_graph_ctx) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        # No assistant message in message_delta -> max_iter branch
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert ctx.state.result is not None
        assert ctx.state.result.stop_reason == StopReason.MAX_ITERATIONS
        assert ctx.state.result.content == "max iterations reached"


class TestAfterTurnNodeRouting:
    async def test_dispatches_after_turn_with_result_before_continuation_routing(
        self,
        make_runtime,
        make_graph_ctx,
    ) -> None:
        node = AfterTurnNode()
        hook = _TrackingAfterTurnHook(node)
        runner = HookRunner()
        runner.add(HookSpec(hook=hook))
        graph_runtime = ReactGraphRuntime(hook_runner=runner)
        tracked_runtime = MagicMock(spec=ReactGraphRuntime)
        tracked_runtime.dispatch_hook = AsyncMock(wraps=graph_runtime.dispatch_hook)
        runtime = make_runtime()
        state = _react_state(runtime)
        _append_assistant(state, "ok")
        state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        ctx = make_graph_ctx(runtime=runtime)
        ctx.runtime = tracked_runtime

        await node.run(ctx)

        assert hook.result is state.result
        tracked_runtime.dispatch_hook.assert_awaited_once()
        call = tracked_runtime.dispatch_hook.await_args
        assert call.args[0] == ReActHookPoint.AFTER_TURN
        assert call.args[1] is ctx
        assert call.args[2] == {"result": state.result}
        delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)
        assert [record.content for record in delivers] == [None]

    async def test_continuation_granted_routes_to_before(
        self, make_runtime, make_graph_ctx
    ) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        state = _react_state(runtime)
        _append_assistant(state, "ok")
        state.turn_attempt = 0  # 0 < default max_turns (1)
        state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)
        assert not ctx.coordinator.collect_consumable_delivers(ReActNode.END, 0)

    async def test_no_continuation_request_routes_to_end(
        self, make_runtime, make_graph_ctx
    ) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        _append_assistant(_react_state(runtime), "ok")
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert ctx.coordinator.collect_consumable_delivers(ReActNode.END, 0)
        assert not ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)

    async def test_turn_attempt_exceeds_max_routes_to_end(
        self, make_runtime, make_graph_ctx
    ) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        state = _react_state(runtime)
        _append_assistant(state, "ok")
        state.turn_attempt = 2
        state.custom[TurnCustomKey.MAX_TURNS] = 2  # 2 < 2 is False
        state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert ctx.coordinator.collect_consumable_delivers(ReActNode.END, 0)
        assert not ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)

    async def test_cancelled_phase_routes_to_end_even_with_flag(
        self, make_runtime, make_graph_ctx
    ) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        state = _react_state(runtime)
        state.phase = TurnPhase.CANCELLED
        state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        state.custom[TurnCustomKey.MAX_TURNS] = 5
        state.turn_attempt = 0
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert ctx.coordinator.collect_consumable_delivers(ReActNode.END, 0)
        assert not ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)

    async def test_continuation_flag_popped_one_shot(
        self, make_runtime, make_graph_ctx
    ) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        state = _react_state(runtime)
        _append_assistant(state, "ok")
        state.turn_attempt = 0
        state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        ctx = make_graph_ctx(runtime=runtime)

        await node.run(ctx)

        assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
        assert ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)

    async def test_continuation_does_not_append_system_reminder(
        self,
        make_runtime,
        make_graph_ctx,
    ) -> None:
        node = AfterTurnNode()
        runtime = make_runtime()
        state = _react_state(runtime)
        _append_assistant(state, "ok")
        state.turn_attempt = 0
        state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
        ctx = make_graph_ctx(runtime=runtime)
        await ctx.agent_ctx.history.append(
            {"role": str(MessageRole.ASSISTANT), "content": "ok"}
        )

        await node.run(ctx)

        messages = await ctx.agent_ctx.history.to_list()
        assert [message.role for message in messages] == [MessageRole.ASSISTANT]
