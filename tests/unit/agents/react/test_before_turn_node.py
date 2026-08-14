# ruff: noqa: ANN001, ANN201, ANN401
"""Tests for BeforeTurnNode."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.nodes.before_turn import BeforeTurnNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.hook import BeforeTurnHook, HookRunner, HookSpec


class _TrackingBeforeTurnHook(BeforeTurnHook):
    def __init__(self, node: BeforeTurnNode) -> None:
        self._node = node
        self.called = False

    async def before_turn(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        assert state is not None
        assert state.turn_attempt == 1
        assert state.current_node == ReActNode.BEFORE
        assert self._node._pending_delivers == []
        self.called = True


class TestBeforeTurnNode:
    @pytest.mark.asyncio
    async def test_increments_turn_attempt_from_zero_to_one(self, make_graph_ctx):
        node = BeforeTurnNode()
        ctx = make_graph_ctx()
        assert ctx.state.turn_attempt == 0

        await node.run(ctx)

        assert ctx.state.turn_attempt == 1

    @pytest.mark.asyncio
    async def test_second_call_increments_turn_attempt_to_two(self, make_graph_ctx):
        node = BeforeTurnNode()
        ctx = make_graph_ctx()

        await node.run(ctx)
        assert ctx.state.turn_attempt == 1
        await node.run(ctx)

        assert ctx.state.turn_attempt == 2

    @pytest.mark.asyncio
    async def test_resets_iteration_to_zero(self, make_graph_ctx):
        node = BeforeTurnNode()
        ctx = make_graph_ctx()
        ctx.state.iteration = 5

        await node.run(ctx)

        assert ctx.state.iteration == 0

    @pytest.mark.asyncio
    async def test_delivers_to_llm(self, make_graph_ctx):
        node = BeforeTurnNode()
        ctx = make_graph_ctx()

        await node.run(ctx)

        assert node._submit_result == {ReActNode.LLM: [None]}

    @pytest.mark.asyncio
    async def test_dispatches_before_turn_after_state_setup_before_deliver(
        self,
        make_graph_ctx,
    ) -> None:
        node = BeforeTurnNode()
        hook = _TrackingBeforeTurnHook(node)
        runner = HookRunner()
        runner.add(HookSpec(hook=hook))
        runtime = ReactGraphRuntime(hook_runner=runner)
        tracked_runtime = MagicMock(spec=ReactGraphRuntime)
        tracked_runtime.dispatch_hook = AsyncMock(wraps=runtime.dispatch_hook)
        ctx = make_graph_ctx()
        ctx.runtime = tracked_runtime

        await node.run(ctx)

        assert hook.called
        tracked_runtime.dispatch_hook.assert_awaited_once_with(
            ReActHookPoint.BEFORE_TURN,
            ctx,
        )
        assert node._submit_result == {ReActNode.LLM: [None]}
