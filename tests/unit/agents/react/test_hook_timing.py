# ruff: noqa: ANN001, ANN202, ANN401
"""Minimal hook timing verification — 4-level hooks fire in order after deliver-ization.

Verifies Q5 (02 ticket): hook firing points are unchanged after the
deliver-ization refactor (tasks 22+23). The 4 levels are:

1. Turn-level: BEFORE_TURN (BeforeTurnNode), AFTER_TURN (AfterTurnNode)
2. Iteration-level: BEFORE_ITERATION / AFTER_ITERATION (LLMNode)
3. Tool-level: BEFORE_TOOL_EXECUTION / AFTER_TOOL_EXECUTION (ToolNode)
4. Node-level: START_NODE_TURN / END_NODE_TURN (StartNode / EndNode) — not
   under test here; this test focuses on the turn + iteration + tool levels.

The test runs a normal turn (BEFORE -> LLM -> TOOL -> LLM -> AFTER) by
calling ``node.run(ctx)`` for each node in sequence, recording the
``dispatch_hook`` calls, and asserting they fire in the expected order.
"""

from __future__ import annotations

from modex_agent import ToolCall
from modex_agent.agents.react.constants import ReActHookPoint, ReActNode
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.nodes.after_turn import AfterTurnNode
from modex_agent.agents.react.nodes.before_turn import BeforeTurnNode
from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.nodes.tool import ToolNode
from modex_agent.agents.react.tool_executor import ToolExecutor
from modex_agent.core.tool_manager import ToolResult


def _make_llm_client() -> ReactLlmClient:
    return ReactLlmClient(provider=object())  # type: ignore[arg-type]


class _MockEmitter:
    def __init__(self) -> None:
        self.events: list = []

    async def emit(self, event, data=None) -> None:
        self.events.append((event, data))

    async def emit_complete(self, result) -> None:
        pass

    async def emit_delta(self, delta) -> None:
        pass

    async def emit_content(self, content) -> None:
        pass

    async def emit_stream_end(self, resuming=False) -> None:
        pass

    def wants_streaming(self) -> bool:
        return False


async def test_four_level_hook_ordering_in_normal_turn(
    make_runtime, make_graph_ctx, make_response
) -> None:
    """Verify 4-level hooks fire in order for a normal turn with one tool call.

    The 4-level hierarchy (turn / iteration / tool / node) is preserved:
      BEFORE_TURN -> [BEFORE_ITERATION -> BEFORE_LLM -> AFTER_LLM_RESPONSE ->
      AFTER_ITERATION] -> [BEFORE_TOOL_EXECUTION -> AFTER_TOOL_EXECUTION] ->
      [BEFORE_ITERATION -> ... -> AFTER_ITERATION] -> AFTER_TURN

    BEFORE_LLM / AFTER_LLM_RESPONSE are sub-hooks within the iteration level
    (also dispatched by LLMNode); they are included to verify the full
    dispatch sequence, but the 4-level ordering is the assertion target.
    """
    hook_calls: list[str] = []

    # Mock LLM: first call returns tool_calls, second returns final answer.
    call_count = 0

    async def _mock_call(messages, ctx):  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return make_response(
                tool_calls=[ToolCall(tool_name="search", arguments={}, call_id="c1")]
            )
        return make_response(content="Done!")

    llm_client = _make_llm_client()
    llm_client.call = _mock_call  # type: ignore[method-assign]

    # Mock tool executor.
    tool_executor = ToolExecutor()

    async def _mock_execute(tc, ctx):  # noqa: ANN001
        return ToolResult.from_text(tc.tool_name, "ok")

    tool_executor.execute = _mock_execute  # type: ignore[method-assign]

    before_node = BeforeTurnNode()
    before_node.node_id = ReActNode.BEFORE.value  # type: ignore[attr-defined]
    llm_node = LLMNode(llm_client, InjectionDrainer())
    llm_node.node_id = ReActNode.LLM.value  # type: ignore[attr-defined]
    tool_node = ToolNode(tool_executor)
    tool_node.node_id = ReActNode.TOOL.value  # type: ignore[attr-defined]
    after_node = AfterTurnNode()
    after_node.node_id = ReActNode.AFTER.value  # type: ignore[attr-defined]

    runtime = make_runtime()
    ctx = make_graph_ctx(runtime=runtime)
    ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

    # Record dispatch_hook calls without actually dispatching (no hooks
    # registered — we only verify the firing order, not hook side-effects).
    async def _record_dispatch(hook_point, _ctx, data=None):  # noqa: ANN001
        hook_calls.append(str(hook_point))

    ctx.runtime.dispatch_hook = _record_dispatch  # type: ignore[method-assign]

    # Run the turn sequence: BEFORE -> LLM -> TOOL -> LLM -> AFTER
    await before_node.run(ctx)
    await llm_node.run(ctx)
    await tool_node.run(ctx)
    await llm_node.run(ctx)
    await after_node.run(ctx)

    expected = [
        str(ReActHookPoint.BEFORE_TURN),
        str(ReActHookPoint.BEFORE_ITERATION),
        str(ReActHookPoint.BEFORE_LLM),
        str(ReActHookPoint.AFTER_LLM_RESPONSE),
        str(ReActHookPoint.AFTER_ITERATION),
        str(ReActHookPoint.BEFORE_TOOL_EXECUTION),
        str(ReActHookPoint.AFTER_TOOL_EXECUTION),
        str(ReActHookPoint.BEFORE_ITERATION),
        str(ReActHookPoint.BEFORE_LLM),
        str(ReActHookPoint.AFTER_LLM_RESPONSE),
        str(ReActHookPoint.AFTER_ITERATION),
        str(ReActHookPoint.AFTER_TURN),
    ]
    assert hook_calls == expected, (
        f"Hook timing mismatch.\nExpected: {expected}\nGot:      {hook_calls}"
    )
