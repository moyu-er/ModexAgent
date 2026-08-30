from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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
from modex_agent.tools.standard.todo_tool import TodoWriteTool


def _make_context(
    root: Path,
    *,
    register_todo_write: bool = True,
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
    if register_todo_write:
        tool_manager.register(TodoWriteTool(store))
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

    await TodoContinuationHook(todo_store=store).after_turn(
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

    await TodoContinuationHook(todo_store=store).after_turn(
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

    await TodoContinuationHook(todo_store=store).after_turn(
        context,
        AgentResult(content="cancelled", stop_reason=StopReason.TURN_CANCELLED),
    )

    await _assert_no_action(context, state)


async def test_missing_tool_manager_still_continues(tmp_path: Path) -> None:
    """The runtime tool-registration gate is dead (todo capability
    migration): enablement is compile-time knowledge, so a missing tool
    manager no longer suppresses the hook — it acts on the store."""
    context, state, store = _make_context(tmp_path)
    context_without_manager = MagicMock(spec=AgentContext)
    context_without_manager.tool_manager = None
    context_without_manager.runtime = context.runtime
    context_without_manager.history = context.history
    context_without_manager.session = context.session
    await _save_todos(
        context,
        store,
        [TodoItem(content="gate death", status=TodoStatus.PENDING)],
    )

    await TodoContinuationHook(todo_store=store).after_turn(
        context_without_manager,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1


async def test_unregistered_todo_write_still_continues(tmp_path: Path) -> None:
    """The scenario the runtime gate used to block — a tool manager
    without ``todo_write`` — now runs: the hook exists only where the
    todo capability is effective (compile-time), so it never probes the
    tool registry."""
    context, state, store = _make_context(tmp_path, register_todo_write=False)
    await _save_todos(
        context,
        store,
        [TodoItem(content="gate death", status=TodoStatus.PENDING)],
    )

    await TodoContinuationHook(todo_store=store).after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    messages = await context.history.to_list()
    assert len(messages) == 1


async def test_none_todo_store_skips_silently(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)

    await TodoContinuationHook(todo_store=None).after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    await _assert_no_action(context, state)


async def test_empty_todos_skip_continuation(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)

    await TodoContinuationHook(todo_store=_store).after_turn(
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
    hook = TodoContinuationHook(todo_store=store)
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
    hook = TodoContinuationHook(todo_store=store)
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
    hook = TodoContinuationHook(todo_store=store)
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


async def test_max_turns_boundary_renews_and_requests_continuation(
    tmp_path: Path,
) -> None:
    context, state, store = _make_context(tmp_path)
    state.turn_attempt = 3
    await _save_todos(
        context,
        store,
        [TodoItem(content="remaining", status=TodoStatus.PENDING)],
    )

    await TodoContinuationHook(todo_store=store).after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS] is True
    assert state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] is not None
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER


async def test_existing_continuation_request_still_injects_reminder(
    tmp_path: Path,
) -> None:
    context, state, store = _make_context(tmp_path)
    state.custom[TurnCustomKey.CONTINUATION_REQUEST] = True
    await _save_todos(
        context,
        store,
        [TodoItem(content="remaining", status=TodoStatus.PENDING)],
    )

    await TodoContinuationHook(todo_store=store).after_turn(
        context,
        AgentResult(content="done", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS] is True
    assert state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] is not None
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER


def _make_tree_mock(tree_id: str | None, active_nodes: list[str]) -> MagicMock:
    tree = MagicMock()
    tree.tree_id_for_session = AsyncMock(return_value=tree_id)
    tree.get_active_subtree_nodes = AsyncMock(return_value=active_nodes)
    return tree


async def test_tree_aware_skips_when_subtree_has_active_nodes(
    tmp_path: Path,
) -> None:
    context, state, store = _make_context(tmp_path)
    await _save_todos(
        context,
        store,
        [TodoItem(content="implement hook", status=TodoStatus.IN_PROGRESS)],
    )
    tree = _make_tree_mock("tree-1", ["session.agent", "child.session"])

    await TodoContinuationHook(tree=tree, todo_store=store).after_turn(
        context,
        AgentResult(content="working", stop_reason=StopReason.COMPLETED),
    )

    await _assert_no_action(context, state)


async def test_tree_aware_triggers_when_subtree_has_only_self(
    tmp_path: Path,
) -> None:
    context, state, store = _make_context(tmp_path)
    await _save_todos(
        context,
        store,
        [TodoItem(content="implement hook", status=TodoStatus.IN_PROGRESS)],
    )
    tree = _make_tree_mock("tree-1", ["session.agent"])

    await TodoContinuationHook(tree=tree, todo_store=store).after_turn(
        context,
        AgentResult(content="working", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] is not None
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER


async def test_tree_none_falls_through(tmp_path: Path) -> None:
    context, state, store = _make_context(tmp_path)
    await _save_todos(
        context,
        store,
        [TodoItem(content="implement hook", status=TodoStatus.IN_PROGRESS)],
    )

    await TodoContinuationHook(todo_store=store).after_turn(
        context,
        AgentResult(content="working", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] is not None
    messages = await context.history.to_list()
    assert len(messages) == 1


async def test_tree_aware_falls_through_when_tree_id_is_none(
    tmp_path: Path,
) -> None:
    context, state, store = _make_context(tmp_path)
    await _save_todos(
        context,
        store,
        [TodoItem(content="implement hook", status=TodoStatus.IN_PROGRESS)],
    )
    tree = _make_tree_mock(None, [])

    await TodoContinuationHook(tree=tree, todo_store=store).after_turn(
        context,
        AgentResult(content="working", stop_reason=StopReason.COMPLETED),
    )

    assert state.custom[TurnCustomKey.CONTINUATION_REQUEST] is True
    assert state.custom[TurnCustomKey.LAST_CONTINUATION_TODO_SIG] is not None
    messages = await context.history.to_list()
    assert len(messages) == 1
