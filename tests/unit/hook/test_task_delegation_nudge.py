"""Tests for `TaskDelegationNudgeHook` — the idle-subagent dispatch nudge.

Covers the deprecated verdict-machine semantics: SHORT_TURN re-arms, gate
failures and USED settle, DUE injects once per armed attempt.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.tool_manager import InMemoryToolManager
from modex_agent.core.types import MessageRole, ToolCall
from modex_agent.hook.builtin.task_delegation_nudge import TaskDelegationNudgeHook
from modex_agent.memory.history import ListMessageHistory
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.tools import (
    CommunicationTarget,
    CommunicationTargetStore,
    TaskDispatchTool,
)
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices


class _StubService:
    async def send_async(self, **kwargs: object) -> str:
        return "ok"


def _task_tool(with_target: bool = True) -> TaskDispatchTool:
    store = CommunicationTargetStore()
    if with_target:
        store.add(CommunicationTarget(name="explore", kind=AgentCommKind.SUBAGENT))
    return TaskDispatchTool(
        store=store,
        source=AgentAddress(name="main"),
        service=_StubService(),  # type: ignore[arg-type]
    )


def _make_context(
    *,
    task_tool: TaskDispatchTool | None,
) -> tuple[AgentContext, ReActTurnState]:
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
    tool_manager = InMemoryToolManager()
    if task_tool is not None:
        tool_manager.register(task_tool)
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
    return context, state


def _tool_call(name: str) -> ToolCall:
    return ToolCall(
        tool_name=name,
        arguments={},
        call_id="call-1",
    )


async def _append(context: AgentContext, message: ChatMessage) -> None:
    await context.history.append(message)


async def test_fresh_turn_entry_does_not_inject() -> None:
    """Regression: iteration zero (no in-turn assistant steps) must not nag."""
    context, state = _make_context(task_tool=_task_tool())
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == [
        ChatMessage(role=MessageRole.USER, content="do stuff")
    ]
    # SHORT_TURN re-arms: later iterations re-evaluate.
    assert state.custom[TurnCustomKey.TASK_NUDGE_PENDING] is True


async def test_short_turn_stays_armed_then_due_injects_once() -> None:
    context, state = _make_context(task_tool=_task_tool())
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)  # 0 assistants — SHORT, re-armed

    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="a"))
    await hook.before_iteration(context)  # 1 assistant — SHORT, re-armed
    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="b"))
    await hook.before_iteration(context)  # 2 assistants — SHORT, re-armed
    assert state.custom[TurnCustomKey.TASK_NUDGE_PENDING] is True

    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="c"))
    await hook.before_iteration(context)  # 3 assistants — DUE, inject + settle

    messages = await context.history.to_list()
    assert len(messages) == 5
    assert messages[-1].role == MessageRole.SYSTEM_REMINDER
    assert "<system-reminder>" in str(messages[-1].content)
    assert "subagents" in str(messages[-1].content)
    assert TurnCustomKey.TASK_NUDGE_PENDING not in state.custom

    await hook.before_iteration(context)  # settled — no re-injection
    assert len(await context.history.to_list()) == 5


async def test_no_task_tool_registered_is_silent() -> None:
    context, state = _make_context(task_tool=None)
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert TurnCustomKey.TASK_NUDGE_PENDING not in state.custom
    assert await context.history.to_list() == []


async def test_empty_roster_is_silent() -> None:
    context, state = _make_context(task_tool=_task_tool(with_target=False))
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert TurnCustomKey.TASK_NUDGE_PENDING not in state.custom
    assert await context.history.to_list() == []


async def test_recent_task_usage_settles_without_injection() -> None:
    context, state = _make_context(task_tool=_task_tool())
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    await _append(
        context, ChatMessage(role=MessageRole.ASSISTANT, content="dispatching")
    )
    await _append(
        context, ChatMessage(role=MessageRole.TOOL, name="task", content="ack")
    )
    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="done"))
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert await context.history.to_list() == [
        ChatMessage(role=MessageRole.USER, content="do stuff"),
        ChatMessage(role=MessageRole.ASSISTANT, content="dispatching"),
        ChatMessage(role=MessageRole.TOOL, name="task", content="ack"),
        ChatMessage(role=MessageRole.ASSISTANT, content="done"),
    ]
    assert TurnCustomKey.TASK_NUDGE_PENDING not in state.custom


async def test_previous_turn_task_usage_does_not_settle_current_turn() -> None:
    """Regression: usage in the previous turn must not suppress this turn."""
    context, state = _make_context(task_tool=_task_tool())
    for content in ("old-a", "old-b", "old-c"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    await _append(
        context, ChatMessage(role=MessageRole.TOOL, name="task", content="ack")
    )
    await _append(context, ChatMessage(role=MessageRole.USER, content="new turn"))
    for content in ("a", "b", "c"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)

    messages = await context.history.to_list()
    assert messages[-1].role == MessageRole.SYSTEM_REMINDER


async def test_previous_turn_tail_alone_does_not_inject() -> None:
    """Regression: previous-turn assistant tail must not satisfy the threshold."""
    context, state = _make_context(task_tool=_task_tool())
    for content in ("old-a", "old-b", "old-c", "old-d"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    await _append(context, ChatMessage(role=MessageRole.USER, content="new turn"))
    await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content="one"))
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)

    messages = await context.history.to_list()
    assert messages[-1].role == MessageRole.ASSISTANT
    assert state.custom[TurnCustomKey.TASK_NUDGE_PENDING] is True


async def test_continuation_flags_are_never_touched() -> None:
    context, state = _make_context(task_tool=_task_tool())
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    for content in ("a", "b", "c"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)

    assert TurnCustomKey.CONTINUATION_REQUEST not in state.custom
    assert TurnCustomKey.CONTINUATION_RENEW_MAX_TURNS not in state.custom


async def test_missing_react_state_is_silent() -> None:
    context, _state = _make_context(task_tool=_task_tool())
    context_no_state = AgentContext(
        system_prompt="test",
        history=context.history,
        tool_manager=context.tool_manager,
        session=context.session,
        identity=None,
    )
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context_no_state)
    await hook.before_iteration(context_no_state)

    assert await context.history.to_list() == []


async def test_assistant_tool_call_alone_does_not_count_as_usage() -> None:
    """Usage detection reads TOOL-role results, not pending assistant calls."""
    context, state = _make_context(task_tool=_task_tool())
    await _append(context, ChatMessage(role=MessageRole.USER, content="do stuff"))
    for content in ("a", "b"):
        await _append(context, ChatMessage(role=MessageRole.ASSISTANT, content=content))
    await _append(
        context,
        ChatMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[_tool_call("task")],
        ),
    )
    hook = TaskDelegationNudgeHook()

    await hook.before_turn(context)
    await hook.before_iteration(context)

    messages = await context.history.to_list()
    assert len(messages) == 5
    assert messages[-1].role == MessageRole.SYSTEM_REMINDER
