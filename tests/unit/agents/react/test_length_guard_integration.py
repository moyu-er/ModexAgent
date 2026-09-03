# ruff: noqa: ANN001
"""LengthGuardHook integration through the real ReAct node sequence.

Follows the ``test_hook_timing`` pattern: run the real nodes
(``BeforeTurnNode`` → ``LLMNode`` → ``AfterTurnNode``) in sequence with a
``HookRunner``-backed ``ReactGraphRuntime``, a fake LLM, and no engine —
verifying the v5 silent-failure fix end to end:

1. A degenerate response (finish_reason=length, empty content, zero tool
   calls) no longer ends the turn as ``COMPLETED``: the guard injects a
   no-thinking nudge system-reminder and the continuation gate routes back
   to ``BEFORE`` (attempt 2).
2. When the second attempt produces a normal response, the final
   ``AgentResult`` is ``COMPLETED`` with content.
3. When every attempt is degenerate, the guard fails honestly: the final
   ``AgentResult`` (the same object ``AfterTurnNode`` wrote to
   ``state.result``) is mutated in place to ``StopReason.ERROR``.
"""

from __future__ import annotations

from modex_agent.agents.react.constants import ReActNode
from modex_agent.agents.react.injection_drainer import InjectionDrainer
from modex_agent.agents.react.llm_client import ReactLlmClient
from modex_agent.agents.react.nodes.after_turn import AfterTurnNode
from modex_agent.agents.react.nodes.before_turn import BeforeTurnNode
from modex_agent.agents.react.nodes.llm import LLMNode
from modex_agent.agents.react.runtime import ReactGraphRuntime
from modex_agent.core.emitter import StopReason
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import MessageRole
from modex_agent.hook import HookRunner, HookSpec
from modex_agent.hook.builtin.length_guard import (
    MAX_NUDGES,
    NUDGE_NO_OUTPUT,
    LengthGuardHook,
)


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


def _make_llm_client(responses: list[LLMResponse]) -> ReactLlmClient:
    client = ReactLlmClient(provider=object())  # type: ignore[arg-type]
    calls: list[LLMResponse] = list(responses)

    async def _mock_call(messages, ctx):  # noqa: ANN001
        assert calls, "LLM called more times than the fake responses provide"
        return calls.pop(0)

    client.call = _mock_call  # type: ignore[method-assign]
    return client


def _make_nodes(llm_client: ReactLlmClient) -> tuple[BeforeTurnNode, LLMNode, AfterTurnNode]:
    before_node = BeforeTurnNode()
    before_node.node_id = ReActNode.BEFORE.value  # type: ignore[attr-defined]
    llm_node = LLMNode(llm_client, InjectionDrainer())
    llm_node.node_id = ReActNode.LLM.value  # type: ignore[attr-defined]
    after_node = AfterTurnNode()
    after_node.node_id = ReActNode.AFTER.value  # type: ignore[attr-defined]
    return before_node, llm_node, after_node


async def test_degenerate_length_turn_continues_then_recovers(
    make_graph_ctx,
) -> None:
    """LENGTH+empty attempt 1 → nudge + continuation; attempt 2 → COMPLETED."""
    llm_client = _make_llm_client(
        [
            LLMResponse(content="", finish_reason=FinishReason.LENGTH),
            LLMResponse(content="Recovered!", finish_reason=FinishReason.STOP),
        ]
    )
    before_node, llm_node, after_node = _make_nodes(llm_client)

    runner = HookRunner()
    runner.add(HookSpec(hook=LengthGuardHook()))
    ctx = make_graph_ctx()
    ctx.runtime = ReactGraphRuntime(hook_runner=runner)
    ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

    # Attempt 1: degenerate ending — guard nudges and the gate routes to BEFORE.
    await before_node.run(ctx)
    await llm_node.run(ctx)
    await after_node.run(ctx)

    assert ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)
    assert not ctx.coordinator.collect_consumable_delivers(ReActNode.END, 0)
    messages = await ctx.agent_ctx.history.to_list()
    reminder = messages[-1]
    assert reminder.role == MessageRole.SYSTEM_REMINDER
    assert NUDGE_NO_OUTPUT in reminder.content

    # Attempt 2: normal response — turn completes with content.
    await before_node.run(ctx)
    await llm_node.run(ctx)
    await after_node.run(ctx)

    assert ctx.state.turn_attempt == 2
    assert ctx.coordinator.collect_consumable_delivers(ReActNode.END, 0)
    assert not ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)
    result = ctx.state.result
    assert result is not None
    assert result.stop_reason == StopReason.COMPLETED
    assert result.content == "Recovered!"


async def test_always_degenerate_provider_fails_honestly_after_exhaustion(
    make_graph_ctx,
) -> None:
    """Every attempt degenerate → 10 nudges, then in-place ERROR on attempt 11."""
    llm_client = _make_llm_client(
        [LLMResponse(content="", finish_reason=FinishReason.LENGTH) for _ in range(MAX_NUDGES + 1)]
    )
    before_node, llm_node, after_node = _make_nodes(llm_client)

    runner = HookRunner()
    runner.add(HookSpec(hook=LengthGuardHook()))
    ctx = make_graph_ctx()
    ctx.runtime = ReactGraphRuntime(hook_runner=runner)
    ctx.agent_ctx.emitter = _MockEmitter()  # type: ignore[assignment]

    for _ in range(MAX_NUDGES + 1):
        await before_node.run(ctx)
        await llm_node.run(ctx)
        await after_node.run(ctx)

    # The exhaustion path mutates the SAME AgentResult AfterTurnNode wrote
    # to state.result — this is what EndNode reads after the gate routes to END.
    result = ctx.state.result
    assert result is not None
    assert result.stop_reason == StopReason.ERROR
    assert result.error == (
        f"length-guard: exhausted {MAX_NUDGES} nudges after degenerate "
        "max_tokens/empty endings with no progress"
    )
    assert ctx.coordinator.collect_consumable_delivers(ReActNode.END, 0)
    assert not ctx.coordinator.collect_consumable_delivers(ReActNode.BEFORE, 0)
    # MAX_NUDGES nudges injected, none on the exhausting attempt.
    messages = await ctx.agent_ctx.history.to_list()
    reminders = [m for m in messages if m.role == MessageRole.SYSTEM_REMINDER]
    assert len(reminders) == MAX_NUDGES
