"""Tests for CompactionReminderHook — cleanup detection and reminder injection."""

from __future__ import annotations

from typing import Any

from modex_agent.core.message import ChatMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import MessageRole, TodoStatus
from modex_agent.hook.builtin.compaction_reminder import CompactionReminderHook
from modex_agent.runtime.enums import AgentKind, TurnCustomKey
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import TodoItem, TodoStore


class _FakeHistory:
    def __init__(self, messages: list[ChatMessage] | None = None) -> None:
        self.messages: list[ChatMessage] = list(messages or [])

    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        self.messages.append(ChatMessage.coerce(message))

    async def to_list(self) -> list[ChatMessage]:
        return list(self.messages)


class _FakeTodoStore(TodoStore):
    def __init__(self, items: list[TodoItem] | None = None) -> None:
        self._items: list[TodoItem] = list(items or [])

    async def get(self, session_id: str) -> list[TodoItem]:
        return list(self._items)

    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        self._items = list(todos)

    async def delete(self, session_id: str) -> None:
        self._items = []


def _make_ctx(
    *,
    history: Any = None,  # noqa: ANN401
    session: str = "test-session",
) -> Any:  # noqa: ANN401
    from modex_agent.agents.react.state import ReActTurnState

    turn_identity = TurnIdentity(
        agent_id="react",
        session=SessionInfo.from_str(session),
        turn_id="t1",
    )
    state = ReActTurnState(
        identity=turn_identity,
        agent_kind=AgentKind.REACT,
    )
    services = AgentRuntimeServices()
    runtime = AgentRuntime(services=services, state=state)

    class _FakeContext:
        def __init__(self) -> None:
            self.history = history or _FakeHistory()
            self.session = SessionInfo.from_str(session)
            self.runtime = runtime
            self.identity = turn_identity

    return _FakeContext()


def _msg(role: MessageRole, content: str) -> ChatMessage:
    return ChatMessage(role=role, content=content)


def _text(msg: ChatMessage) -> str:
    return str(msg.content or "")


async def test_no_injection_on_first_call() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, "hello")])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    assert len(history.messages) == 1
    snapshot = ctx.runtime.state.custom.get(TurnCustomKey.COMPACTION_PREV_SNAPSHOT)
    assert snapshot is not None
    assert snapshot["len"] == 1


async def test_detects_length_halved() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, "survivor") for _ in range(20)]
    await hook.before_iteration(ctx)

    assert len(history.messages) == 21
    assert "<system-reminder>" in _text(history.messages[-1])


async def test_detects_length_drop_over_20() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, f"msg {i}") for i in range(29)]
    await hook.before_iteration(ctx)

    assert "<system-reminder>" in _text(history.messages[-1])


async def test_detects_head_fingerprint_change() -> None:
    history = _FakeHistory(
        [_msg(MessageRole.USER, "original first message")]
        + [_msg(MessageRole.ASSISTANT, f"reply {i}") for i in range(5)]
    )
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages = [
        _msg(MessageRole.USER, "completely different first message"),
        _msg(MessageRole.ASSISTANT, "reply 0"),
    ]
    await hook.before_iteration(ctx)

    assert "<system-reminder>" in _text(history.messages[-1])


async def test_no_false_positive_on_growth() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, "hello")])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages.append(_msg(MessageRole.ASSISTANT, "response"))
    await hook.before_iteration(ctx)

    assert len(history.messages) == 2
    assert not any("<system-reminder>" in _text(m) for m in history.messages)


async def test_no_false_positive_on_small_shrink() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(30)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, f"msg {i}") for i in range(28)]
    await hook.before_iteration(ctx)

    assert len(history.messages) == 28
    assert not any("<system-reminder>" in _text(m) for m in history.messages)


async def test_reminder_contains_archive_mention() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook(has_archive=True)

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, "survivor")]
    await hook.before_iteration(ctx)

    reminder = _text(history.messages[-1])
    assert "archive summaries" in reminder
    assert "highest number" in reminder


async def test_reminder_no_archive_when_disabled() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook(has_archive=False)

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, "survivor")]
    await hook.before_iteration(ctx)

    reminder = _text(history.messages[-1])
    assert "archive summaries" not in reminder


async def test_reminder_contains_todos() -> None:
    todos = [
        TodoItem(content="task A", status=TodoStatus.IN_PROGRESS),
        TodoItem(content="task B", status=TodoStatus.PENDING),
    ]
    todo_store = _FakeTodoStore(todos)
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook(todo_store=todo_store, has_todo_tool=True)

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, "survivor")]
    await hook.before_iteration(ctx)

    reminder = _text(history.messages[-1])
    assert "current active todos" in reminder
    assert "[in_progress] task A" in reminder
    assert "[pending] task B" in reminder
    assert "todo_read" in reminder


async def test_reminder_no_todos_when_empty() -> None:
    todo_store = _FakeTodoStore([])
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook(todo_store=todo_store, has_todo_tool=True)

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, "survivor")]
    await hook.before_iteration(ctx)

    reminder = _text(history.messages[-1])
    assert "current active todos" not in reminder
    assert "Continue your work." in reminder


async def test_reminder_no_todos_when_tool_absent() -> None:
    todo_store = _FakeTodoStore(
        [
            TodoItem(content="task A", status=TodoStatus.PENDING),
        ]
    )
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook(todo_store=todo_store, has_todo_tool=False)

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, "survivor")]
    await hook.before_iteration(ctx)

    reminder = _text(history.messages[-1])
    assert "current active todos" not in reminder


async def test_snapshot_updated_after_detection() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, "survivor")]
    await hook.before_iteration(ctx)

    snapshot = ctx.runtime.state.custom.get(TurnCustomKey.COMPACTION_PREV_SNAPSHOT)
    assert snapshot is not None
    assert snapshot["len"] == 2


async def test_no_state_no_op() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, "hello")])

    class _NoStateContext:
        def __init__(self) -> None:
            self.history = history
            self.session = SessionInfo.from_str("test")
            self.runtime = None
            self.identity = None

    ctx: Any = _NoStateContext()
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    assert len(history.messages) == 1


async def test_empty_history_does_not_crash() -> None:
    history = _FakeHistory()
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    snapshot = ctx.runtime.state.custom.get(TurnCustomKey.COMPACTION_PREV_SNAPSHOT)
    assert snapshot is not None
    assert snapshot["len"] == 0
    assert snapshot["fp"] is None


async def test_pruned_transcript_mention() -> None:
    history = _FakeHistory([_msg(MessageRole.USER, f"msg {i}") for i in range(50)])
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, "survivor")]
    await hook.before_iteration(ctx)

    reminder = _text(history.messages[-1])
    assert "pruned transcript catalog" in reminder


async def test_no_false_positive_on_growth_from_empty() -> None:
    history = _FakeHistory()
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages.append(_msg(MessageRole.USER, "first message"))
    await hook.before_iteration(ctx)

    assert len(history.messages) == 1
    assert not any("<system-reminder>" in _text(m) for m in history.messages)


class _CleanupSimulatingHistory:
    """Fake history that prunes oldest messages when exceeding a threshold.

    Simulates ScopedMessageHistory._run_cleanup() behavior: when append pushes
    length over ``cleanup_threshold``, oldest messages are pruned down to
    ``keep_count``.
    """

    def __init__(
        self,
        messages: list[ChatMessage] | None = None,
        cleanup_threshold: int = 50,
        keep_count: int = 20,
    ) -> None:
        self.messages: list[ChatMessage] = list(messages or [])
        self._cleanup_threshold = cleanup_threshold
        self._keep_count = keep_count

    async def append(self, message: ChatMessage | dict[str, Any]) -> None:
        self.messages.append(ChatMessage.coerce(message))
        if len(self.messages) > self._cleanup_threshold:
            self.messages = self.messages[-self._keep_count:]

    async def to_list(self) -> list[ChatMessage]:
        return list(self.messages)


async def test_snapshot_re_read_after_injection_prevents_cascade() -> None:
    history = _CleanupSimulatingHistory(
        [_msg(MessageRole.USER, f"msg {i}") for i in range(50)],
        cleanup_threshold=50,
        keep_count=20,
    )
    ctx = _make_ctx(history=history)
    hook = CompactionReminderHook()

    await hook.before_iteration(ctx)

    history.messages = [_msg(MessageRole.USER, f"msg {i}") for i in range(20)]
    await hook.before_iteration(ctx)

    snapshot = ctx.runtime.state.custom.get(TurnCustomKey.COMPACTION_PREV_SNAPSHOT)
    assert snapshot is not None
    assert snapshot["len"] == 21

    await hook.before_iteration(ctx)

    reminders = [m for m in history.messages if "<system-reminder>" in _text(m)]
    assert len(reminders) == 1
