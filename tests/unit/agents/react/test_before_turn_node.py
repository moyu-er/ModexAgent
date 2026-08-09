# ruff: noqa: ANN401
"""Tests for BeforeTurnNode."""

from __future__ import annotations

import pytest

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.nodes.before_turn import BeforeTurnNode


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
