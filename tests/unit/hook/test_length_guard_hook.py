from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import FinishReason, StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import LLMResponse, MessageRole, ToolCall
from modex_agent.hook.builtin.length_guard import (
    MAX_NUDGES,
    NUDGE_NO_OUTPUT,
    NUDGE_TRUNCATED,
    LengthGuardHook,
)
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager


def _make_context() -> tuple[AgentContext, ReActTurnState]:
    identity = TurnIdentity(
        agent_id="test",
        session=SessionInfo.from_str("session.agent"),
        turn_id="turn-1",
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        turn_attempt=1,
    )
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    context = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=identity.session,
        runtime=runtime,
        graph_context=MagicMock(),
        identity=identity,
    )
    return context, state


async def _run_degenerate_turn(
    hook: LengthGuardHook,
    context: AgentContext,
    *,
    finish_reason: FinishReason,
    content: str | None,
) -> AgentResult:
    """after_llm_response + after_turn for one degenerate/completed attempt."""
    await hook.after_llm_response(
        context, LLMResponse(content=content, finish_reason=finish_reason)
    )
    result = AgentResult(content=content or "", stop_reason=StopReason.COMPLETED)
    await hook.after_turn(context, result)
    return result


async def test_case_a_length_empty_completed_nudges_and_requests_continuation() -> None:
    context, state = _make_context()
    hook = LengthGuardHook()

    await _run_degenerate_turn(hook, context, finish_reason=FinishReason.LENGTH, content="")

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS] is True
    assert state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] == 1
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    assert NUDGE_NO_OUTPUT in messages[0].content


async def test_case_a_prime_stop_empty_completed_nudges_and_requests_continuation() -> None:
    context, state = _make_context()
    hook = LengthGuardHook()

    await _run_degenerate_turn(hook, context, finish_reason=FinishReason.STOP, content=None)

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS] is True
    assert state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] == 1
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    assert NUDGE_NO_OUTPUT in messages[0].content


async def test_case_b_length_with_content_injects_truncated_nudge() -> None:
    context, state = _make_context()
    hook = LengthGuardHook()
    truncated = "Here is the beginning of my answer that got cut"

    await _run_degenerate_turn(hook, context, finish_reason=FinishReason.LENGTH, content=truncated)

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS] is True
    assert state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] == 1
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    assert NUDGE_TRUNCATED in messages[0].content


async def test_progress_resets_counter_between_degenerate_attempts() -> None:
    context, state = _make_context()
    hook = LengthGuardHook()

    await _run_degenerate_turn(hook, context, finish_reason=FinishReason.LENGTH, content="")
    assert state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] == 1
    state.custom.pop(TurnCustomKey.CONTINUATION_REQUEST)
    state.custom.pop(TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS)

    # Productive response resets the counter to zero.
    await hook.after_llm_response(
        context, LLMResponse(content="made progress", finish_reason=FinishReason.STOP)
    )
    assert state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] == 0

    # Next degenerate attempt counts 1 again, not 2.
    await _run_degenerate_turn(hook, context, finish_reason=FinishReason.LENGTH, content="")
    assert state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] == 1
    # Two nudges total (one per degenerate attempt), no duplication.
    assert len(await context.history.to_list()) == 2


async def test_exhaustion_mutates_result_in_place_to_honest_error() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] = MAX_NUDGES
    hook = LengthGuardHook()

    result = await _run_degenerate_turn(
        hook, context, finish_reason=FinishReason.LENGTH, content=""
    )

    assert result.stop_reason == StopReason.ERROR
    assert result.error == (
        f"length-guard: exhausted {MAX_NUDGES} nudges after degenerate "
        "max_tokens/empty endings with no progress"
    )
    # No continuation flags — the gate must route to END with the mutated result.
    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    assert TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS not in state.custom
    # No nudge injected on the exhaustion path.
    assert await context.history.to_list() == []


async def test_not_triggered_for_terminal_results_and_normal_stop() -> None:
    context, state = _make_context()
    hook = LengthGuardHook()
    await hook.after_llm_response(
        context, LLMResponse(content="", finish_reason=FinishReason.LENGTH)
    )

    # ERROR / TURN_CANCELLED / MAX_ITERATIONS results are untouched.
    for stop_reason in (
        StopReason.ERROR,
        StopReason.TURN_CANCELLED,
        StopReason.MAX_ITERATIONS,
    ):
        result = AgentResult(content="", stop_reason=stop_reason)
        await hook.after_turn(context, result)
        assert result.stop_reason == stop_reason
        assert result.error is None

    # STOP with non-empty content is a normal completion.
    state.custom[TurnCustomKey.LAST_LLM_FINISH_REASON] = FinishReason.STOP
    await hook.after_turn(
        context, AgentResult(content="all done", stop_reason=StopReason.COMPLETED)
    )

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    assert TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS not in state.custom
    assert await context.history.to_list() == []


async def test_after_llm_response_records_finish_reason_and_resets_on_output() -> None:
    context, state = _make_context()
    hook = LengthGuardHook()

    # Degenerate response: record finish_reason, counter untouched.
    await hook.after_llm_response(
        context, LLMResponse(content="", finish_reason=FinishReason.LENGTH)
    )
    assert state.custom[TurnCustomKey.LAST_LLM_FINISH_REASON] == FinishReason.LENGTH
    assert TurnCustomKey.LENGTH_GUARD_NUDGES not in state.custom

    # Productive content resets the counter.
    state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] = 3
    await hook.after_llm_response(
        context, LLMResponse(content="hello", finish_reason=FinishReason.STOP)
    )
    assert state.custom[TurnCustomKey.LAST_LLM_FINISH_REASON] == FinishReason.STOP
    assert state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] == 0

    # Productive tool calls also reset (empty content).
    state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] = 5
    await hook.after_llm_response(
        context,
        LLMResponse(
            content="",
            tool_calls=[ToolCall(tool_name="search", arguments={}, call_id="c1")],
            finish_reason=FinishReason.TOOL_CALLS,
        ),
    )
    assert state.custom[TurnCustomKey.LAST_LLM_FINISH_REASON] == FinishReason.TOOL_CALLS
    assert state.custom[TurnCustomKey.LENGTH_GUARD_NUDGES] == 0


async def test_returns_cleanly_without_react_state() -> None:
    # runtime=None (and identity=None) -> get_react_state returns None.
    context = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=SessionInfo.from_str("session.agent"),
    )
    hook = LengthGuardHook()

    await hook.after_llm_response(
        context, LLMResponse(content="", finish_reason=FinishReason.LENGTH)
    )
    await hook.after_turn(context, AgentResult(content="", stop_reason=StopReason.COMPLETED))

    assert await context.history.to_list() == []
