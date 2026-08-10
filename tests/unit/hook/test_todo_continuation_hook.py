from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.constants import StopReason
from modex_agent.core.emitter import AgentResult
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import MessageRole, TodoStatus
from modex_agent.hook.builtin.todo_continuation import TodoContinuationHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import JsonFileTodoStore, TodoItem
from modex_agent.tools.standard.todo_tool import TodoReadTool


def _make_context(
    root: Path,
    *,
    register_todo_read: bool = True,
) -> tuple[AgentContext, ReActTurnState, JsonFileTodoStore]:
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
    state.custom[TurnCustomKey.MAX_TURNS] = 3
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
    store = JsonFileTodoStore(root)
    tool_manager = InMemoryToolManager()
    if register_todo_read:
        tool_manager.register(TodoReadTool(store))
    context = AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=tool_manager,
        session=identity.session,
        runtime=runtime,
        graph_context=MagicMock(),
        identity=identity,
    )
    return context, state, store


async def _save_todos(
    context: AgentContext,
    store: JsonFileTodoStore,
    todos: list[TodoItem],
) -> None:
    await store.save(str(context.session), todos)


async def _assert_no_action(context: AgentContext, state: ReActTurnState) -> None:
    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    assert TurnCustomKey.LAST_CONTINUATION_TODO_SIG not in state.custom
    assert await context.history.to_list() == []


async def test_first_active_todo_requests_continuation_and_caches_signature(
    tmp_path: Path,
) -> None:
    context, state, store = _make_context(tmp_path)
    await _save_todos(
        context,
        store,
        [TodoItem(content="implement hook", status=TodoStatus.IN_PROGRESS)],
    )

    await TodoContinuationHook().after_turn(
        context,
        AgentResult(content="working", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert len(state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG]) == 16
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER


async def test_max_iterations_with_active_todo_requests_continuation(
    tmp_path: Path,
) -> None:
    context, state, store = _make_context(tmp_path)
    await _save_todos(
        context,
        store,
        [TodoItem(content="continue work", status=TodoStatus.PENDING)],
    )

    await TodoContinuationHook().after_turn(
        context,
        AgentResult(content="limit", stop_reason=StopReason.MAX_ITERATIONS),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True


async def test_cancelled_result_does_nothing(tmp_path: Path) -> None:
    context, state, store = _make_context(tmp_path)
    await _save_todos(
        context,
        store,
        [TodoItem(content="remaining", status=TodoStatus.PENDING)],
    )

    await TodoContinuationHook().after_turn(
        context,
        AgentResult(content="cancelled", stop_reason=StopReason.TURN_CANCELLED),
    )

    await _assert_no_action(context, state)


async def test_missing_tool_manager_skips_silently(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)
    context_without_manager = MagicMock(spec=AgentContext)
    context_without_manager.tool_manager = None
    context_without_manager.runtime = context.runtime
    context_without_manager.history = context.history
    context_without_manager.session = context.session

    await TodoContinuationHook().after_turn(
        context_without_manager,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    await _assert_no_action(context, state)


async def test_unregistered_todo_read_skips_silently(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path, register_todo_read=False)

    await TodoContinuationHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    await _assert_no_action(context, state)


async def test_empty_todos_skip_continuation(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)

    await TodoContinuationHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    await _assert_no_action(context, state)


async def test_unchanged_cached_signature_skips_continuation(tmp_path: Path) -> None:
    context, state, store = _make_context(tmp_path)
    await _save_todos(
        context,
        store,
        [TodoItem(content="same work", status=TodoStatus.IN_PROGRESS)],
    )
    hook = TodoContinuationHook()
    result = AgentResult(content="working", stop_reason=StopReason.COMPLETED)
    await hook.after_turn(context, result)
    cached_signature = state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG]
    state.custom.pop(TurnCustomKey.CONTINUATION_REQUEST)

    await hook.after_turn(context, result)

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    assert state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] == cached_signature
    assert len(await context.history.to_list()) == 1


async def test_completed_item_changes_signature_and_retriggers(tmp_path: Path) -> None:
    context, state, store = _make_context(tmp_path)
    hook = TodoContinuationHook()
    result = AgentResult(content="working", stop_reason=StopReason.COMPLETED)
    await _save_todos(
        context,
        store,
        [
            TodoItem(content="first", status=TodoStatus.IN_PROGRESS),
            TodoItem(content="second", status=TodoStatus.PENDING),
        ],
    )
    await hook.after_turn(context, result)
    old_signature = state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG]
    state.custom.pop(TurnCustomKey.CONTINUATION_REQUEST)
    await _save_todos(
        context,
        store,
        [
            TodoItem(content="first", status=TodoStatus.COMPLETED),
            TodoItem(content="second", status=TodoStatus.IN_PROGRESS),
        ],
    )

    await hook.after_turn(context, result)

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] != old_signature


async def test_added_todo_changes_signature_and_retriggers(tmp_path: Path) -> None:
    context, state, store = _make_context(tmp_path)
    hook = TodoContinuationHook()
    result = AgentResult(content="working", stop_reason=StopReason.COMPLETED)
    await _save_todos(
        context,
        store,
        [TodoItem(content="first", status=TodoStatus.IN_PROGRESS)],
    )
    await hook.after_turn(context, result)
    old_signature = state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG]
    state.custom.pop(TurnCustomKey.CONTINUATION_REQUEST)
    await _save_todos(
        context,
        store,
        [
            TodoItem(content="first", status=TodoStatus.IN_PROGRESS),
            TodoItem(content="new", status=TodoStatus.PENDING),
        ],
    )

    await hook.after_turn(context, result)

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] != old_signature


async def test_max_turns_boundary_skips_continuation(tmp_path: Path) -> None:
    context, state, store = _make_context(tmp_path)
    state.turn_attempt = 3
    await _save_todos(
        context,
        store,
        [TodoItem(content="remaining", status=TodoStatus.PENDING)],
    )

    await TodoContinuationHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    await _assert_no_action(context, state)


async def test_existing_continuation_request_skips_todo_continuation(
    tmp_path: Path,
) -> None:
    context, state, store = _make_context(tmp_path)
    state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
    await _save_todos(
        context,
        store,
        [TodoItem(content="remaining", status=TodoStatus.PENDING)],
    )

    await TodoContinuationHook().after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert TurnCustomKey.LAST_CONTINUATION_TODO_SIG not in state.custom
    assert await context.history.to_list() == []
