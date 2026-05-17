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
    c = OverflowCleaner(store, merge_window=0.01)
    yield c
    await c.stop()


class TestCleanerRemovesExpiredCallIds:
    @pytest.mark.asyncio
    async def test_cleaner_removes_expired_call_ids(
        self, tmp_path: Path, cleaner: OverflowCleaner, store: LocalFileToolOverflowStore,
    ) -> None:
        for i in range(3):
            await store.store(
                session_id="sess_1",
                tool_call_id=f"call_{i}",
                tool_name="read_file",
                content=f"content_{i}" * 10,
            )

        ids_before = await store.list_tool_call_ids("sess_1")
        assert len(ids_before) == 3

        # Cleanup: keep call_1 and call_2
        cleaner.schedule_cleanup("sess_1", {"call_1", "call_2"})
        await cleaner.flush()

        ids_after = await store.list_tool_call_ids("sess_1")
        assert ids_after == ["call_1", "call_2"]


class TestCleanerEnforcesMaxCount:
    @pytest.mark.asyncio
    async def test_cleaner_enforces_max_count(
        self, tmp_path: Path, cleaner: OverflowCleaner, store: LocalFileToolOverflowStore,
    ) -> None:
        for i in range(10):
            await store.store(
                session_id="sess_1",
                tool_call_id=f"call_{i}",
                tool_name="read_file",
                content=f"c{i}" * 5,
            )

        ids_before = await store.list_tool_call_ids("sess_1")
        assert len(ids_before) == 10

        cleaner.schedule_cleanup("sess_1", set(ids_before), max_tool_call_ids=3)
        await cleaner.flush()

        ids_after = await store.list_tool_call_ids("sess_1")
        assert ids_after == ["call_7", "call_8", "call_9"]


class TestCleanerMergeSameSessionRequests:
    @pytest.mark.asyncio
    async def test_cleaner_merge_same_session_requests(
        self, tmp_path: Path, cleaner: OverflowCleaner, store: LocalFileToolOverflowStore,
    ) -> None:
        for i in range(4):
            await store.store(
                session_id="sess_1",
                tool_call_id=f"call_{i}",
                tool_name="read_file",
                content=f"c{i}" * 5,
            )

        # Two quick schedules for same session — merge_window timer
        # combines both kept sets before flush fires
        cleaner.schedule_cleanup("sess_1", {"call_0"})
        cleaner.schedule_cleanup("sess_1", {"call_1"})
        await cleaner.flush()

        ids_after = await store.list_tool_call_ids("sess_1")
        assert ids_after == ["call_0", "call_1"]


class TestCleanerStopDoesNotCrash:
    @pytest.mark.asyncio
    async def test_cleaner_stop_does_not_crash(
        self, tmp_path: Path, store: LocalFileToolOverflowStore,
    ) -> None:
        c = OverflowCleaner(store)

        await store.store(
            session_id="sess_1",
            tool_call_id="call_0",
            tool_name="read_file",
            content="content" * 10,
        )
        c.schedule_cleanup("sess_1", {"call_0"})

        await c.stop()  # flushes pending then stops
