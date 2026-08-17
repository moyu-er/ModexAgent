# ruff: noqa: ANN001
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.nodes.start import StartNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.agents.react.state import get_react_state
from modex_agent.core.agent import AgentContext
from modex_agent.hook import HookRunner, HookSpec, StartNodeTurnHook
from modex_agent.runtime.enums import TurnPhase


class _TrackingStartNodeTurnHook(StartNodeTurnHook):
    def __init__(self, node: StartNode) -> None:
        self._node = node
        self.called = False

    async def start_node_turn(self, ctx: AgentContext) -> None:
        state = get_react_state(ctx)
        assert state is not None
        assert state.phase == TurnPhase.RUNNING
        assert state.current_node == ReActNode.START
        self.called = True


def _tracking_runtime(hook: StartNodeTurnHook) -> MagicMock:
    runner = HookRunner()
    runner.add(HookSpec(hook=hook))
    runtime = ReactGraphRuntime(hook_runner=runner)
    tracked = MagicMock(spec=ReactGraphRuntime)
    tracked.emit = AsyncMock(wraps=runtime.emit)
    tracked.dispatch_hook = AsyncMock(wraps=runtime.dispatch_hook)
    return tracked


async def test_fresh_start_dispatches_start_node_turn(
    make_runtime,
    make_graph_ctx,
) -> None:
    node = StartNode()
    hook = _TrackingStartNodeTurnHook(node)
    assert isinstance(hook, StartNodeTurnHook)
    ctx = make_graph_ctx(runtime=make_runtime())
    ctx.runtime = _tracking_runtime(hook)

    await node.run(ctx)

    assert hook.called
    ctx.runtime.dispatch_hook.assert_awaited_once_with(
        ReActHookPoint.START_NODE_TURN,
        ctx,
    )
    delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)
    assert [record.content for record in delivers] == [None]


async def test_resume_does_not_dispatch_start_node_turn(
    make_runtime,
    make_graph_ctx,
) -> None:
    node = StartNode()
    hook = _TrackingStartNodeTurnHook(node)
    assert isinstance(hook, StartNodeTurnHook)
    runtime = make_runtime()
    runtime.state.resume_target = ReActNode.TOOL
    ctx = make_graph_ctx(runtime=runtime)
    ctx.runtime = _tracking_runtime(hook)

    await node.run(ctx)

    assert not hook.called
    ctx.runtime.dispatch_hook.assert_not_awaited()
    delivers = ctx.coordinator.collect_consumable_delivers(ReActNode.TOOL, 0)
    assert [record.content for record in delivers] == [None]
