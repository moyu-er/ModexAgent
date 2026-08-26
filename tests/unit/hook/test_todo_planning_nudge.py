"""Tests for `TodoPlanningNudgeHook` — the empty-todo planning nudge."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import MessageRole, TodoStatus
from modex_agent.hook.builtin.todo_planning_nudge import TodoPlanningNudgeHook
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
        session=SessionInfo.from_str("session.main"),
        turn_id="turn-1",
    )
    state = ReActTurnState(
        identity=identity,
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.RUNNING,
        turn_attempt=1,
    )
    store = JsonFileTodoStore(root)
    tool_manager = InMemoryToolManager()
    if register_todo_write:
        tool_manager.register(TodoWriteTool(store))
    runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
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


async def test_empty_todo_and_no_usage_injects_once(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.before_turn(context)

    assert state.custom[TurnCustomKey.TODO_NUDGE_PENDING] is True

    await hook.before_iteration(context)

    assert TurnCustomKey.TODO_NUDGE_PENDING not in state.custom
    messages = await context.history.to_list()
    assert len(messages) == 1
    assert messages[0].role == MessageRole.SYSTEM_REMINDER
    content = str(messages[0].content)
    assert "<system-reminder>" in content
    assert "todo_write" in content


async def test_second_iteration_same_attempt_does_not_reinject(tmp_path: Path) -> None:
    context, _state, _store = _make_context(tmp_path)
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.before_turn(context)
    await hook.before_iteration(context)
    await hook.before_iteration(context)

    assert len(await context.history.to_list()) == 1


async def test_any_existing_todo_item_suppresses(tmp_path: Path) -> None:
    context, _state, store = _make_context(tmp_path)
    await store.save(
        str(context.session),
        [TodoItem(content="done earlier", status=TodoStatus.COMPLETED)],
    )
    hook = TodoPlanningNudgeHook(todo_store=store)

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == []


async def test_missing_todo_write_tool_is_silent(tmp_path: Path) -> None:
    context, _state, store = _make_context(tmp_path, register_todo_write=False)
    hook = TodoPlanningNudgeHook(todo_store=store)

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == []


async def test_none_todo_store_is_silent(tmp_path: Path) -> None:
    context, _state, _store = _make_context(tmp_path)
    hook = TodoPlanningNudgeHook(todo_store=None)

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == []


async def test_recent_todo_usage_suppresses(tmp_path: Path) -> None:
    context, _state, _store = _make_context(tmp_path)
    await context.history.append(
        ChatMessage(role=MessageRole.ASSISTANT, content="planning")
    )
    await context.history.append(
        ChatMessage(role=MessageRole.TOOL, name="todo_write", content="ok")
    )
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.before_turn(context)
    await hook.before_iteration(context)

    messages = await context.history.to_list()
    assert len(messages) == 2


async def test_continuation_flags_are_never_touched(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    assert TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS not in state.custom


async def test_missing_react_state_is_silent(tmp_path: Path) -> None:
    context, _state, _store = _make_context(tmp_path)
    context_no_state = AgentContext(
        system_prompt="test",
        history=context.history,
        tool_manager=context.tool_manager,
        session=context.session,
        identity=None,
    )
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.before_turn(context_no_state)
    await hook.before_iteration(context_no_state)

    assert await context.history.to_list() == []
