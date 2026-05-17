from __future__ import annotations

import pytest
from pathlib import Path

from framework.tools.overflow.cleaner import OverflowCleaner
from framework.tools.overflow.local import LocalFileToolOverflowStore


@pytest.fixture
async def store(tmp_path: Path) -> LocalFileToolOverflowStore:
    s = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
    await s.initialize()
    return s


@pytest.fixture
async def cleaner(store: LocalFileToolOverflowStore) -> OverflowCleaner:
    # Use a tiny merge window for fast tests
    c = OverflowCleaner(store, merge_window=0.01)
    await c.start()
    yield c
    await c.stop()


class TestCleanerRemovesExpiredCallIds:
    @pytest.mark.asyncio
    async def test_cleaner_removes_expired_call_ids(self, tmp_path: Path, cleaner: OverflowCleaner, store: LocalFileToolOverflowStore) -> None:
        # Create 3 entries
        for i in range(3):
            await store.store(
                session_id="sess_1",
                tool_call_id=f"call_{i}",
                tool_name="read_file",
                content=f"content_{i}" * 10,
            )

        ids_before = await store.list_tool_call_ids("sess_1")
        assert len(ids_before) == 3

        # Schedule cleanup keeping 2
        cleaner.schedule_cleanup("sess_1", {"call_1", "call_2"})
        await cleaner.flush()

        ids_after = await store.list_tool_call_ids("sess_1")
        assert ids_after == ["call_1", "call_2"]


class TestCleanerEnforcesMaxCount:
    @pytest.mark.asyncio
    async def test_cleaner_enforces_max_count(self, tmp_path: Path, cleaner: OverflowCleaner, store: LocalFileToolOverflowStore) -> None:
        # Create 10 entries with small delays to ensure ordering
        for i in range(10):
            await store.store(
                session_id="sess_1",
                tool_call_id=f"call_{i}",
                tool_name="read_file",
                content=f"c{i}" * 5,
            )

        ids_before = await store.list_tool_call_ids("sess_1")
        assert len(ids_before) == 10

        # Keep all but max=3 — oldest 7 should be deleted
        cleaner.schedule_cleanup("sess_1", set(ids_before), max_tool_call_ids=3)
        await cleaner.flush()

        ids_after = await store.list_tool_call_ids("sess_1")
        # Oldest 7 deleted, newest 3 remain
        assert ids_after == ["call_7", "call_8", "call_9"]


class TestCleanerMergeSameSessionRequests:
    @pytest.mark.asyncio
    async def test_cleaner_merge_same_session_requests(self, tmp_path: Path, cleaner: OverflowCleaner, store: LocalFileToolOverflowStore) -> None:
        # Create 4 entries
        for i in range(4):
            await store.store(
                session_id="sess_1",
                tool_call_id=f"call_{i}",
                tool_name="read_file",
                content=f"c{i}" * 5,
            )

        # Schedule twice for same session with different kept sets
        cleaner.schedule_cleanup("sess_1", {"call_0"})
        cleaner.schedule_cleanup("sess_1", {"call_1"})
        await cleaner.flush()

        ids_after = await store.list_tool_call_ids("sess_1")
        # Merged kept = {call_0, call_1}, so call_2 and call_3 deleted
        assert ids_after == ["call_0", "call_1"]


class TestCleanerStopDoesNotCrash:
    @pytest.mark.asyncio
    async def test_cleaner_stop_does_not_crash(self, tmp_path: Path, store: LocalFileToolOverflowStore) -> None:
        c = OverflowCleaner(store)
        await c.start()

        # Create an entry and schedule cleanup
        await store.store(
            session_id="sess_1",
            tool_call_id="call_0",
            tool_name="read_file",
            content="content" * 10,
        )
        c.schedule_cleanup("sess_1", {"call_0"})

        # Stop gracefully
        await c.stop()

        # Verify stop completed without exception
        assert c._worker is None
