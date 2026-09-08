from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.message import MessageRole
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import Tool
from modex_agent.hook.builtin.deliver_retry import DeliverRetryHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager


class _StubDeliverTool(Tool):
    """Minimal Tool named 'deliver' for guard-existence tests."""

    async def execute(self, **kwargs: Any) -> Any:  # noqa: ANN401 - Tool ABC contract
        return None


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
    tool_manager = InMemoryToolManager()
    tool_manager.register(_StubDeliverTool(name="deliver"))
    context = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=tool_manager,
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


async def test_terminal_attempt_injects_reminder_without_flag() -> None:
    context, state = _make_context()
    state.turn_attempt = 2

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    _assert_unchanged(state)
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    assert "deliver" in messages[0].content


async def test_no_deliver_tool_returns_early() -> None:
    context, state = _make_context()
    context.tool_manager = InMemoryToolManager()

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    _assert_unchanged(state)


async def test_none_tool_manager_returns_early() -> None:
    context, state = _make_context()
    context.tool_manager = None  # type: ignore[assignment]

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    _assert_unchanged(state)


def _make_tree_mock(tree_id: str | None, active_nodes: list[str]) -> MagicMock:
    tree = MagicMock()
    tree.tree_id_for_session = AsyncMock(return_value=tree_id)
    tree.get_active_subtree_nodes = AsyncMock(return_value=active_nodes)
    return tree


async def test_tree_aware_skips_when_subtree_has_active_nodes() -> None:
    context, state = _make_context()
    tree = _make_tree_mock("tree-1", ["session.agent", "child.session"])

    await DeliverRetryHook(tree=tree).after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    _assert_unchanged(state)
    assert await context.history.to_list() == []


async def test_tree_aware_triggers_when_subtree_has_only_self() -> None:
    context, state = _make_context()
    tree = _make_tree_mock("tree-1", ["session.agent"])

    await DeliverRetryHook(tree=tree).after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    assert "deliver" in messages[0].content


async def test_tree_none_falls_through() -> None:
    context, state = _make_context()

    await DeliverRetryHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER


async def test_tree_aware_falls_through_when_tree_id_is_none() -> None:
    context, state = _make_context()
    tree = _make_tree_mock(None, [])

    await DeliverRetryHook(tree=tree).after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
