from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import MessageRole
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


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
    state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] = 0
    state.custom[TurnCustomKey.MAX_TURNS] = 2
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


def _assert_unchanged(state: ReActTurnState) -> None:
    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom


async def test_stop_without_deliver_requests_continuation() -> None:
    context, state = _make_context()

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    assert "deliver" in messages[0].content
    assert "MUST call" in messages[0].content


async def test_max_iterations_without_deliver_requests_continuation() -> None:
    context, state = _make_context()

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="limit reached", stop_reason=StopReason.MAX_ITERATIONS),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True


async def test_delivered_response_does_nothing() -> None:
    context, state = _make_context()
    state.custom[TurnCustomKey.GRAPH_DELIVER_COUNT] = 1

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    _assert_unchanged(state)


async def test_cancelled_result_does_nothing() -> None:
    context, state = _make_context()

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="partial", stop_reason=StopReason.TURN_CANCELLED),
    )

    _assert_unchanged(state)


async def test_error_result_does_nothing() -> None:
    context, state = _make_context()
    result = AgentResult(error="failed", stop_reason=StopReason.ERROR)

    await DeliverRetryHook().after_turn(context, result)

    _assert_unchanged(state)


async def test_missing_react_state_does_nothing() -> None:
    context, state = _make_context()
    context.runtime = None

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    _assert_unchanged(state)


async def test_terminal_attempt_does_nothing() -> None:
    context, state = _make_context()
    state.turn_attempt = 2

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    _assert_unchanged(state)
