"""Tests for ``TodoPlanningNudgeHook`` — the empty-todo planning nudge.

Covers the verdict-machine semantics: SHORT_TURN re-arms, gate failures
(existing todos, missing ``todo_write``, ``None`` store) and USED settle,
DUE injects once per armed logical turn. Arming authority is
``start_node_turn`` (fresh turns only) — the two deprecation root-cause
regressions pin exactly that.
"""

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


async def _append(context: AgentContext, message: ChatMessage) -> None:
    await context.history.append(message)


async def test_fresh_turn_entry_does_not_inject(tmp_path: Path) -> None:
    """Regression: iteration zero (no in-turn assistant steps) must not nag."""
    context, state, _store = _make_context(tmp_path)
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.start_node_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == [
        ChatMessage(role=MessageRole.USER, content="do stuff")
    ]
    assert state.custom[TurnCustomKey.TODO_NUDGE_PENDING] is True


async def test_short_turn_stays_armed_then_due_injects_once(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.start_node_turn(context)
    await hook.before_iteration(context)  # 0 assistants → SHORT, re-armed

    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="a"))
    await hook.before_iteration(context)  # 1 assistant → SHORT, re-armed
    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="b"))
    await hook.before_iteration(context)  # 2 assistants → SHORT, re-armed
    assert state.custom[TurnCustomKey.TODO_NUDGE_PENDING] is True

    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="c"))
    await hook.before_iteration(context)  # 3 assistants → DUE, inject + settle

    messages = await context.history.to_list()
    assert len(messages) == 5
    assert messages[-1].role == MessageRole.SYSTEM_REMINDER
    assert TurnCustomKey.TODO_NUDGE_PENDING not in state.custom

    await hook.before_iteration(context)  # settled → no re-injection
    assert len(await context.history.to_list()) == 5


async def test_any_existing_todo_item_settles(tmp_path: Path) -> None:
    context, state, store = _make_context(tmp_path)
    await store.save(
        str(context.session),
        [TodoItem(content="done earlier", status=TodoStatus.COMPLETED)],
    )
    hook = TodoPlanningNudgeHook(todo_store=store)

    await hook.start_node_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == []
    assert TurnCustomKey.TODO_NUDGE_PENDING not in state.custom


async def test_missing_todo_write_tool_is_silent(tmp_path: Path) -> None:
    context, state, store = _make_context(tmp_path, register_todo_write=False)
    hook = TodoPlanningNudgeHook(todo_store=store)

    await hook.start_node_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == []
    assert TurnCustomKey.TODO_NUDGE_PENDING not in state.custom


async def test_none_todo_store_is_silent(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)
    hook = TodoPlanningNudgeHook(todo_store=None)

    await hook.start_node_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == []
    assert TurnCustomKey.TODO_NUDGE_PENDING not in state.custom


async def test_recent_todo_usage_settles_without_injection(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="planning"))
    await _append(
        context, ChatMessage(role=MessageRole.TOOL, name="todo_write", content="ok")
    )
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.start_node_turn(context)
    await hook.before_iteration(context)

    messages = await context.history.to_list()
    assert len(messages) == 3
    assert TurnCustomKey.TODO_NUDGE_PENDING not in state.custom


async def test_previous_turn_tail_alone_does_not_inject(tmp_path: Path) -> None:
    """Regression: previous-turn assistant tail must not satisfy the threshold."""
    context, state, _store = _make_context(tmp_path)
    for content in ("old-a", "old-b", "old-c", "old-d"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    await _append(context, ChatMessage(role=MessageRole.USER, content="new turn"))
    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="one"))
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.start_node_turn(context)
    await hook.before_iteration(context)

    assert len(await context.history.to_list()) == 6
    assert state.custom[TurnCustomKey.TODO_NUDGE_PENDING] is True


async def test_continuation_flags_are_never_touched(tmp_path: Path) -> None:
    context, state, _store = _make_context(tmp_path)
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    for content in ("a", "b", "c"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.start_node_turn(context)
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

    await hook.start_node_turn(context_no_state)
    await hook.before_iteration(context_no_state)

    assert await context.history.to_list() == []


async def test_no_rearm_after_settle_within_logical_turn(tmp_path: Path) -> None:
    """The deprecation root-cause regression: only ``start_node_turn`` arms.

    After the DUE injection settles the flag, further iterations of the
    same logical turn — even with more assistant steps accumulating and
    still no todo tool usage — must NOT re-inject. The retired
    ``before_turn`` per-attempt arming double-nudged exactly here."""
    context, state, _store = _make_context(tmp_path)
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    for content in ("a", "b", "c"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    await hook.start_node_turn(context)
    await hook.before_iteration(context)  # DUE → inject once → settled

    for content in ("d", "e", "f", "g"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    for _ in range(3):
        await hook.before_iteration(context)  # no arming dispatch → inert

    messages = await context.history.to_list()
    reminders = [m for m in messages if m.role == MessageRole.SYSTEM_REMINDER]
    assert len(reminders) == 1
    assert TurnCustomKey.TODO_NUDGE_PENDING not in state.custom


async def test_armed_flag_only_set_by_start_node_turn(tmp_path: Path) -> None:
    """``before_iteration`` alone (no arming dispatch) is inert — proves
    the arming authority is the fresh-turn node point, not the attempt
    point (the retired ``before_turn`` hook point)."""
    context, state, _store = _make_context(tmp_path)
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    for content in ("a", "b", "c", "d"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    hook = TodoPlanningNudgeHook(todo_store=JsonFileTodoStore(tmp_path))

    # No start_node_turn call — before_iteration must never inject.
    for _ in range(3):
        await hook.before_iteration(context)

    messages = await context.history.to_list()
    assert all(m.role != MessageRole.SYSTEM_REMINDER for m in messages)
    assert TurnCustomKey.TODO_NUDGE_PENDING not in state.custom
