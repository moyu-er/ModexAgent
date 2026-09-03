# ruff: noqa: ANN001
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.agents.react.constants import ReActHookPoint
from modex_agent.agents.react.nodes.end import EndNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.hook import EndNodeTurnHook, HookRunner, HookSpec
from modex_agent.runtime.enums import TurnPhase
from modex_graph.constants import GraphNode


class _TrackingEndNodeTurnHook(EndNodeTurnHook):
    def __init__(self, node: EndNode, result: AgentResult) -> None:
        self._node = node
        self._result = result
        self.called = False

    async def end_node_turn(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        assert state is not None
        assert state.phase == TurnPhase.COMPLETED
        assert state.result is self._result
        self.called = True


async def test_end_dispatches_end_node_turn_before_deliver(
    make_runtime,
    make_graph_ctx,
) -> None:
    node = EndNode()
    result = AgentResult(content="done", stop_reason=StopReason.COMPLETED)
    hook = _TrackingEndNodeTurnHook(node, result)
    assert isinstance(hook, EndNodeTurnHook)
    runner = HookRunner()
    runner.add(HookSpec(hook=hook))
    runtime = ReactGraphRuntime(hook_runner=runner)
    tracked_runtime = MagicMock(spec=ReactGraphRuntime)
    tracked_runtime.emit = AsyncMock(wraps=runtime.emit)
    tracked_runtime.dispatch_hook = AsyncMock(wraps=runtime.dispatch_hook)
    ctx = make_graph_ctx(runtime=make_runtime())
    ctx.state.result = result
    ctx.runtime = tracked_runtime

    await node.run(ctx)

    assert hook.called
    tracked_runtime.dispatch_hook.assert_awaited_once_with(
        ReActHookPoint.END_NODE_TURN,
        ctx,
    )
    delivers = ctx.coordinator.collect_consumable_delivers(GraphNode.END, 0)
    assert [record.content for record in delivers] == [None]
