"""Tests for dynamic behavior injectors (TodoListReminderInjector, PostCompactionRefreshInjector)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from modex_agent.agents.react.injectors import (
    PostCompactionRefreshInjector,
    TodoListReminderInjector,
)
from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import current_agent_context
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import TodoStatus
from modex_agent.runtime.enums import AgentKind
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.runtime.store import TodoItem

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeHistory:
    """In-memory async history for tests, with cleanup-listeners support."""

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages: list[dict[str, Any]] = list(messages or [])
        self._cleanup_listeners: list[Any] = []

    async def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    async def to_list(self) -> list[dict[str, Any]]:
        return list(self.messages)


class _FakeTodoStore:
    """In-memory TodoStore for tests."""

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
    history: _FakeHistory | None = None,
    state: ReActTurnState | None = None,
    session: str = "test-session",
) -> Any:  # noqa: ANN401
    """Build a minimal AgentContext-like object for hook tests."""
    state = state or ReActTurnState(
        identity=TurnIdentity(
            agent_id="react",
            session=SessionInfo.from_str(session),
            turn_id="t1",
        ),
        agent_kind=AgentKind.REACT,
    )
    services = AgentRuntimeServices()
    runtime = AgentRuntime(services=services, state=state)

    class _FakeContext:
        def __init__(self) -> None:
            self.history = history or _FakeHistory()
            self.session = SessionInfo.from_str(session)
            self.runtime = runtime
            self.identity = state.identity

    ctx = _FakeContext()
    return ctx


def _todo(content: str, status: TodoStatus) -> TodoItem:
    return TodoItem(content=content, status=status)


def _make_cleanup_result(
    triggered: bool = True,
    messages_pruned: int = 15,
) -> Any:
    """Build a minimal CleanupResult-like object."""
    class _FakeResult:
        def __init__(self) -> None:
            self.triggered = triggered
            self.messages_pruned = messages_pruned
    return _FakeResult()


def _make_memory_context(session_id: str = "test-session") -> Any:
    """Build a minimal MemoryContext-like object."""
    class _FakeMemCtx:
        def __init__(self) -> None:
            self.session_id = session_id
    return _FakeMemCtx()


# ---------------------------------------------------------------------------
# TodoListReminderInjector
# ---------------------------------------------------------------------------


class TestTodoListReminderInjector:
    """Tests for TodoListReminderInjector.before_iteration."""

    @pytest.mark.asyncio
    async def test_no_reminder_on_first_iteration(self) -> None:
        """Iteration 0 → no reminder (too soon)."""
        store = _FakeTodoStore([_todo("task A", TodoStatus.PENDING)])
        hook = TodoListReminderInjector(store)
        ctx = _make_ctx()
        await hook.before_iteration(ctx)
        assert len(ctx.history.messages) == 0

    @pytest.mark.asyncio
    async def test_reminder_after_interval(self) -> None:
        """Reminder injected after reminder_interval iterations."""
        store = _FakeTodoStore([_todo("task A", TodoStatus.IN_PROGRESS)])
        hook = TodoListReminderInjector(store, reminder_interval=3)
        ctx = _make_ctx()
        state = ctx.runtime.state
        # Advance to iteration 3
        state.iteration = 3
        await hook.before_iteration(ctx)
        injected = [m for m in ctx.history.messages if "system-reminder" in str(m.get("content", ""))]
        assert len(injected) == 1
        assert "task A" in injected[0]["content"]
        assert "in_progress" in injected[0]["content"]

    @pytest.mark.asyncio
    async def test_no_reminder_when_no_active_todos(self) -> None:
        """No active todos → no reminder."""
        store = _FakeTodoStore([_todo("done", TodoStatus.COMPLETED)])
        hook = TodoListReminderInjector(store, reminder_interval=1)
        ctx = _make_ctx()
        ctx.runtime.state.iteration = 5
        await hook.before_iteration(ctx)
        assert len(ctx.history.messages) == 0

    @pytest.mark.asyncio
    async def test_max_reminders_cap(self) -> None:
        """At most max_reminders per turn."""
        store = _FakeTodoStore([_todo("task", TodoStatus.PENDING)])
        hook = TodoListReminderInjector(store, reminder_interval=1, max_reminders=2)
        ctx = _make_ctx()
        state = ctx.runtime.state
        for i in range(1, 10):
            state.iteration = i
            await hook.before_iteration(ctx)
        injected = [m for m in ctx.history.messages if "system-reminder" in str(m.get("content", ""))]
        assert len(injected) == 2

    @pytest.mark.asyncio
    async def test_reminder_interval_respected(self) -> None:
        """Reminders are spaced by reminder_interval."""
        store = _FakeTodoStore([_todo("task", TodoStatus.PENDING)])
        hook = TodoListReminderInjector(store, reminder_interval=5, max_reminders=10)
        ctx = _make_ctx()
        state = ctx.runtime.state
        for i in range(1, 21):
            state.iteration = i
            await hook.before_iteration(ctx)
        injected = [m for m in ctx.history.messages if "system-reminder" in str(m.get("content", ""))]
        # Iterations 5, 10, 15, 20 → 4 reminders
        assert len(injected) == 4

    @pytest.mark.asyncio
    async def test_no_reminder_when_no_react_state(self) -> None:
        """No ReActTurnState → no reminder."""
        store = _FakeTodoStore([_todo("task", TodoStatus.PENDING)])
        hook = TodoListReminderInjector(store, reminder_interval=1)
        ctx = _make_ctx()
        # Simulate non-ReAct state
        ctx.runtime.state = object()
        await hook.before_iteration(ctx)
        assert len(ctx.history.messages) == 0


# ---------------------------------------------------------------------------
# PostCompactionRefreshInjector
# ---------------------------------------------------------------------------


class TestPostCompactionRefreshInjector:
    """Tests for PostCompactionRefreshInjector (BeforeTurnHook + MemoryCleanupListener)."""

    @pytest.mark.asyncio
    async def test_before_turn_registers_listener(self) -> None:
        """before_turn registers self on history._cleanup_listeners."""
        injector = PostCompactionRefreshInjector()
        history = _FakeHistory()
        ctx = _make_ctx(history=history)

        await injector.before_turn(ctx)

        assert injector in history._cleanup_listeners

    @pytest.mark.asyncio
    async def test_before_turn_idempotent(self) -> None:
        """Calling before_turn twice does not double-register."""
        injector = PostCompactionRefreshInjector()
        history = _FakeHistory()
        ctx = _make_ctx(history=history)

        await injector.before_turn(ctx)
        await injector.before_turn(ctx)

        assert history._cleanup_listeners.count(injector) == 1

    @pytest.mark.asyncio
    async def test_no_injection_without_agent_context(self) -> None:
        """on_cleanup_finished skips when no agent context (between-turn cleanup)."""
        injector = PostCompactionRefreshInjector()
        mem_ctx = _make_memory_context()
        result = _make_cleanup_result()

        # No current_agent_context set → should skip
        token = current_agent_context.set(None)  # explicitly clear
        try:
            await injector.on_cleanup_finished(mem_ctx, result)
        finally:
            current_agent_context.reset(token)

    @pytest.mark.asyncio
    async def test_no_injection_when_not_triggered(self) -> None:
        """on_cleanup_finished skips when result.triggered is False."""
        injector = PostCompactionRefreshInjector()
        ctx = _make_ctx()
        token = current_agent_context.set(ctx)
        try:
            result = _make_cleanup_result(triggered=False, messages_pruned=0)
            mem_ctx = _make_memory_context()
            await injector.on_cleanup_finished(mem_ctx, result)
            assert len(ctx.history.messages) == 0
        finally:
            current_agent_context.reset(token)

    @pytest.mark.asyncio
    async def test_no_injection_when_zero_pruned(self) -> None:
        """on_cleanup_finished skips when messages_pruned is 0."""
        injector = PostCompactionRefreshInjector()
        ctx = _make_ctx()
        token = current_agent_context.set(ctx)
        try:
            result = _make_cleanup_result(triggered=True, messages_pruned=0)
            mem_ctx = _make_memory_context()
            await injector.on_cleanup_finished(mem_ctx, result)
            assert len(ctx.history.messages) == 0
        finally:
            current_agent_context.reset(token)

    @pytest.mark.asyncio
    async def test_injection_on_cleanup_with_agent_context(self) -> None:
        """on_cleanup_finished injects reminder when agent context is active."""
        injector = PostCompactionRefreshInjector()
        ctx = _make_ctx()
        token = current_agent_context.set(ctx)
        try:
            result = _make_cleanup_result(triggered=True, messages_pruned=15)
            mem_ctx = _make_memory_context()
            await injector.on_cleanup_finished(mem_ctx, result)

            assert len(ctx.history.messages) == 1
            msg = ctx.history.messages[0]
            assert "compacted" in msg["content"].lower()
            assert "pruned" in msg["content"].lower()
        finally:
            current_agent_context.reset(token)

    @pytest.mark.asyncio
    async def test_injection_with_todos(self) -> None:
        """Reminder includes active todo items when todo_store has them."""
        store = _FakeTodoStore([
            _todo("Write tests", TodoStatus.IN_PROGRESS),
            _todo("Review PR", TodoStatus.PENDING),
            _todo("Done task", TodoStatus.COMPLETED),
        ])
        injector = PostCompactionRefreshInjector(todo_store=store)
        ctx = _make_ctx()
        token = current_agent_context.set(ctx)
        try:
            result = _make_cleanup_result()
            mem_ctx = _make_memory_context()
            await injector.on_cleanup_finished(mem_ctx, result)

            assert len(ctx.history.messages) == 1
            content = ctx.history.messages[0]["content"]
            assert "Write tests" in content
            assert "Review PR" in content
            assert "in_progress" in content
            assert "pending" in content
            assert "Done task" not in content
            assert "todo_read" in content
        finally:
            current_agent_context.reset(token)

    @pytest.mark.asyncio
    async def test_injection_with_empty_todos(self) -> None:
        """No active todos → compaction notice only, no todo section."""
        store = _FakeTodoStore([_todo("done", TodoStatus.COMPLETED)])
        injector = PostCompactionRefreshInjector(todo_store=store)
        ctx = _make_ctx()
        token = current_agent_context.set(ctx)
        try:
            result = _make_cleanup_result()
            mem_ctx = _make_memory_context()
            await injector.on_cleanup_finished(mem_ctx, result)

            assert len(ctx.history.messages) == 1
            content = ctx.history.messages[0]["content"]
            assert "compacted" in content.lower()
            assert "active todos" not in content.lower()
        finally:
            current_agent_context.reset(token)

    @pytest.mark.asyncio
    async def test_injection_without_todo_store(self) -> None:
        """No todo_store → compaction notice only."""
        injector = PostCompactionRefreshInjector(todo_store=None)
        ctx = _make_ctx()
        token = current_agent_context.set(ctx)
        try:
            result = _make_cleanup_result()
            mem_ctx = _make_memory_context()
            await injector.on_cleanup_finished(mem_ctx, result)

            assert len(ctx.history.messages) == 1
            content = ctx.history.messages[0]["content"]
            assert "compacted" in content.lower()
            assert "active todos" not in content.lower()
        finally:
            current_agent_context.reset(token)

    @pytest.mark.asyncio
    async def test_reentrancy_guard(self) -> None:
        """The reminder's own append-cleanup should not trigger another injection."""
        injector = PostCompactionRefreshInjector()
        ctx = _make_ctx()
        token = current_agent_context.set(ctx)
        try:
            result = _make_cleanup_result()
            mem_ctx = _make_memory_context()

            # Make append simulate a cleanup callback (re-entrant call)
            original_append = ctx.history.append

            async def _reentrant_append(message):
                await original_append(message)
                # Simulate cleanup firing from the append
                await injector.on_cleanup_finished(mem_ctx, result)

            ctx.history.append = _reentrant_append

            # First call — should inject once, and the re-entrant append
            # should NOT inject again (guarded by _injecting flag)
            await injector.on_cleanup_finished(mem_ctx, result)
            assert len(ctx.history.messages) == 1
        finally:
            current_agent_context.reset(token)
