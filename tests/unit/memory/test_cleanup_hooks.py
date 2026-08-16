"""Tests for ``modex_agent.memory.cleanup_hooks.TodoReorientationHook``.

Verifies the event-driven post-cleanup reminder:
  - Fires only when ``cleanup_result.messages_pruned > 0``.
  - Reads PENDING + IN_PROGRESS todos and includes them in the reminder.
  - Persists via ``SessionMemoryManager.add_messages`` (Path A — no recorder,
    no ``_run_cleanup`` re-entry, no ``write_id``).
  - Bypasses ``MemoryAppendRecorder`` / ``MemoryProvider`` fan-out.
  - Visible to a ``ScopedMessageHistory`` backed by the same session manager.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import MemoryContext
from modex_agent.core.types import MessageRole, TodoStatus
from modex_agent.memory.cleanup import CleanupResult
from modex_agent.memory.cleanup_hooks import TodoReorientationHook
from modex_agent.memory.core.layers import SessionMemoryManager
from modex_agent.memory.default_system import ScopedMessageHistory
from modex_agent.memory.hooks import MemoryHookContext
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.recorder import MemoryAppendRecorder
from modex_agent.memory.registry import DefaultMemoryStoreRegistry
from modex_agent.plugins.abc import MemoryProvider
from modex_agent.runtime.store import TodoItem, TodoStore

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeTodoStore(TodoStore):
    """In-memory todo store keyed by session_id — no file I/O."""

    def __init__(self) -> None:
        self._data: dict[str, list[TodoItem]] = {}

    async def save(self, session_id: str, todos: list[TodoItem]) -> None:
        self._data[session_id] = list(todos)

    async def get(self, session_id: str) -> list[TodoItem]:
        return list(self._data.get(session_id, []))

    async def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)


class _RecordingProvider(MemoryProvider):
    """Records every ``add()`` call — verifies no provider fan-out."""

    def __init__(self) -> None:
        self.add_calls: list[tuple[list[ChatMessage], MemoryContext]] = []

    @property
    def name(self) -> str:
        return "recording"

    async def initialize(self, **kwargs: Any) -> None:  # noqa: ANN401
        pass

    async def shutdown(self) -> None:
        pass

    async def add(
        self,
        messages: list[ChatMessage],
        context: MemoryContext,
    ) -> dict[str, Any]:
        self.add_calls.append((list(messages), context))
        return {"status": "ok", "memories": []}

    async def search(
        self,
        query: str,
        context: MemoryContext,
        limit: int = 5,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(session_id: str = "reorient-session") -> MemoryContext:
    return MemoryContext(session_id=session_id, user_id="test-user")


def _finished(pruned: int = 5, triggered: bool = True) -> CleanupResult:
    return CleanupResult(
        triggered=triggered,
        messages_kept=3,
        messages_pruned=pruned,
    )


def _hook_ctx(
    *,
    session_manager: SessionMemoryManager | None = None,
    memory_context: MemoryContext | None = None,
    cleanup_result: CleanupResult | None = None,
) -> MemoryHookContext:
    return MemoryHookContext(
        session_manager=session_manager,
        memory_context=memory_context,
        cleanup_result=cleanup_result,
    )


def _reminder_messages(msgs: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in msgs if m.role == MessageRole.SYSTEM_REMINDER and "<system-reminder>" in (m.content or "")]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> DefaultMemoryStoreRegistry:
    return DefaultMemoryStoreRegistry(tmp_path)


@pytest.fixture
def session_manager(registry: DefaultMemoryStoreRegistry) -> SessionMemoryManager:
    layers = MemoryLayerFactory.single_user(registry=registry)
    return layers.session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestActiveTodosAppear:
    """Active todos appear in persisted reminder; completed/cancelled excluded."""

    async def test_pending_and_in_progress_appear_completed_excluded(
        self,
        session_manager: SessionMemoryManager,
    ) -> None:
        ctx = _ctx()
        todo_store = _FakeTodoStore()
        await todo_store.save(
            ctx.session_id or "",
            [
                TodoItem(content="task A pending", status=TodoStatus.PENDING),
                TodoItem(content="task B in progress", status=TodoStatus.IN_PROGRESS),
                TodoItem(content="task C completed", status=TodoStatus.COMPLETED),
                TodoItem(content="task D cancelled", status=TodoStatus.CANCELLED),
            ],
        )
        hook = TodoReorientationHook(todo_store)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=4),
        )

        await hook.on_cleanup_finished(hook_ctx)

        msgs = await session_manager.get_all_messages(ctx)
        reminders = _reminder_messages(msgs)
        assert len(reminders) == 1
        body = reminders[0].content or ""
        assert "task A pending" in body
        assert "task B in progress" in body
        assert "task C completed" not in body
        assert "task D cancelled" not in body
        assert "[pending]" in body
        assert "[in_progress]" in body


class TestCompletedCancelledExcluded:
    """Only PENDING + IN_PROGRESS appear in the todo section."""

    async def test_no_active_todos_falls_back_to_generic_reminder(
        self,
        session_manager: SessionMemoryManager,
    ) -> None:
        ctx = _ctx()
        todo_store = _FakeTodoStore()
        await todo_store.save(
            ctx.session_id or "",
            [
                TodoItem(content="done", status=TodoStatus.COMPLETED),
                TodoItem(content="scrapped", status=TodoStatus.CANCELLED),
            ],
        )
        hook = TodoReorientationHook(todo_store)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=3),
        )

        await hook.on_cleanup_finished(hook_ctx)

        msgs = await session_manager.get_all_messages(ctx)
        reminders = _reminder_messages(msgs)
        assert len(reminders) == 1
        body = reminders[0].content or ""
        assert "Your current active todos" not in body
        assert "Continue your work." in body


class TestEmptyOrNoStorePersistsGeneric:
    """Empty/no store still persists a generic 'Continue your work' reminder."""

    async def test_no_store_generic_reminder(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(todo_store=None)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=2),
        )

        await hook.on_cleanup_finished(hook_ctx)

        msgs = await session_manager.get_all_messages(ctx)
        reminders = _reminder_messages(msgs)
        assert len(reminders) == 1
        assert "Continue your work." in (reminders[0].content or "")

    async def test_empty_todo_store_generic_reminder(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(_FakeTodoStore())
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=1),
        )

        await hook.on_cleanup_finished(hook_ctx)

        msgs = await session_manager.get_all_messages(ctx)
        reminders = _reminder_messages(msgs)
        assert len(reminders) == 1
        assert "Continue your work." in (reminders[0].content or "")


class TestNoWriteOnPrunedZero:
    """``messages_pruned == 0`` produces no write."""

    async def test_pruned_zero_no_reminder(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(_FakeTodoStore())
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=CleanupResult(triggered=True, messages_pruned=0),
        )

        await hook.on_cleanup_finished(hook_ctx)

        msgs = await session_manager.get_all_messages(ctx)
        assert _reminder_messages(msgs) == []


class TestMissingContextFieldsNoWrite:
    """Missing session_manager / memory_context / cleanup_result → no-op."""

    async def test_no_cleanup_result(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(_FakeTodoStore())
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=None,
        )
        await hook.on_cleanup_finished(hook_ctx)
        msgs = await session_manager.get_all_messages(ctx)
        assert _reminder_messages(msgs) == []

    async def test_no_session_manager(self) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(_FakeTodoStore())
        hook_ctx = _hook_ctx(
            session_manager=None,
            memory_context=ctx,
            cleanup_result=_finished(),
        )
        await hook.on_cleanup_finished(hook_ctx)  # must not raise

    async def test_no_memory_context(self, session_manager: SessionMemoryManager) -> None:
        hook = TodoReorientationHook(_FakeTodoStore())
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=None,
            cleanup_result=_finished(),
        )
        await hook.on_cleanup_finished(hook_ctx)  # must not raise


class TestNonTriggeredNoWrite:
    """``triggered=False`` → no-op, even with pruned > 0."""

    async def test_non_triggered_no_reminder(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(_FakeTodoStore())
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=CleanupResult(triggered=False, messages_pruned=5),
        )

        await hook.on_cleanup_finished(hook_ctx)

        msgs = await session_manager.get_all_messages(ctx)
        assert _reminder_messages(msgs) == []


class TestNoRecursiveCleanup:
    """Reminder insertion triggers no second cleanup invocation.

    ``SessionMemoryManager.add_messages`` (Path A) does not call
    ``_run_cleanup`` or dispatch ``CLEANUP_FINISHED`` again. Verified by
    counting persisted reminder messages: exactly one, not many.
    """

    async def test_single_reminder_no_recursion(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        todo_store = _FakeTodoStore()
        await todo_store.save(
            ctx.session_id or "",
            [TodoItem(content="active task", status=TodoStatus.PENDING)],
        )
        hook = TodoReorientationHook(todo_store)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=10),
        )

        await hook.on_cleanup_finished(hook_ctx)

        msgs = await session_manager.get_all_messages(ctx)
        reminders = _reminder_messages(msgs)
        assert len(reminders) == 1


class TestCacheVisibility:
    """``ScopedMessageHistory.to_list()`` sees the persisted reminder after cache refresh.

    After the enclosing ``history.append()`` returns (cache incrementally updated),
    the hook persists via Path A. When compact triggers, ``_refresh_cache`` reads
    fresh from the session manager and sees the reminder. This test simulates the
    compact path by calling ``_refresh_cache()`` manually after the hook fires.
    """

    async def test_history_to_list_contains_reminder(
        self,
        session_manager: SessionMemoryManager,
    ) -> None:
        ctx = _ctx()
        history = ScopedMessageHistory(
            manager=session_manager,
            context=ctx,
        )
        await history.append(ChatMessage(role=MessageRole.USER, content="hello"))

        hook = TodoReorientationHook(todo_store=None)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=2),
        )
        await hook.on_cleanup_finished(hook_ctx)
        await history._refresh_cache()  # Simulate compact-triggered cache refresh

        msgs = await history.to_list()
        reminders = _reminder_messages(msgs)
        assert len(reminders) == 1
        assert "Continue your work." in (reminders[0].content or "")


class TestNoProviderFanOut:
    """A recording ``MemoryProvider`` receives no reorientation message.

    The hook persists via ``SessionMemoryManager.add_messages`` (Path A —
    no recorder). Even when a ``MemoryAppendRecorder`` with a provider
    exists in the environment, the provider's ``add()`` is never called
    for the reorientation message.
    """

    async def test_provider_not_called_for_reminder(
        self,
        session_manager: SessionMemoryManager,
    ) -> None:
        ctx = _ctx()
        provider = _RecordingProvider()
        recorder = MemoryAppendRecorder()
        recorder.add_provider(provider)

        hook = TodoReorientationHook(todo_store=None)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=3),
        )
        await hook.on_cleanup_finished(hook_ctx)

        msgs = await session_manager.get_all_messages(ctx)
        reminders = _reminder_messages(msgs)
        assert len(reminders) == 1
        assert provider.add_calls == []

    async def test_recorder_via_history_append_still_sees_only_user_message(
        self,
        session_manager: SessionMemoryManager,
    ) -> None:
        """Contrast: ScopedMessageHistory.append (Path C) DOES record.

        The hook does not use that path, so a single ``history.append``
        yields exactly one provider call (the user message) — the
        reorientation reminder is not recorded because the hook bypasses
        the recorder.
        """
        ctx = _ctx()
        provider = _RecordingProvider()
        recorder = MemoryAppendRecorder()
        recorder.add_provider(provider)
        history = ScopedMessageHistory(
            manager=session_manager,
            context=ctx,
            recorder=recorder,
        )
        await history.append(ChatMessage(role=MessageRole.USER, content="hello"))
        await recorder.flush()

        before = len(provider.add_calls)
        assert before == 1  # Path C recorded the user message

        hook = TodoReorientationHook(todo_store=None)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=1),
        )
        await hook.on_cleanup_finished(hook_ctx)

        after = len(provider.add_calls)
        assert after == before  # no additional provider fan-out


# ---------------------------------------------------------------------------
# Wording
# ---------------------------------------------------------------------------


class TestWording:
    """``has_archive`` toggles the archive-summaries paragraph in the reminder."""

    async def test_has_archive_adds_archive_paragraph(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(todo_store=None, has_archive=True)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=1),
        )
        await hook.on_cleanup_finished(hook_ctx)
        msgs = await session_manager.get_all_messages(ctx)
        body = _reminder_messages(msgs)[0].content or ""
        assert "archive summaries" in body
        assert "highest" in body

    async def test_no_archive_omits_archive_paragraph(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(todo_store=None, has_archive=False)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=1),
        )
        await hook.on_cleanup_finished(hook_ctx)
        msgs = await session_manager.get_all_messages(ctx)
        body = _reminder_messages(msgs)[0].content or ""
        assert "archive summaries" not in body

    async def test_reminder_is_user_role_and_no_write_id(self, session_manager: SessionMemoryManager) -> None:
        ctx = _ctx()
        hook = TodoReorientationHook(todo_store=None)
        hook_ctx = _hook_ctx(
            session_manager=session_manager,
            memory_context=ctx,
            cleanup_result=_finished(pruned=1),
        )
        await hook.on_cleanup_finished(hook_ctx)
        msgs = await session_manager.get_all_messages(ctx)
        reminder = _reminder_messages(msgs)[0]
        assert reminder.role == MessageRole.SYSTEM_REMINDER
        # ChatMessage has extra="allow"; verify write_id was never set via the serialized form.
        dumped = reminder.model_dump()
        assert not dumped.get("write_id")
