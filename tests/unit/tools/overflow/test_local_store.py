from __future__ import annotations

import asyncio
import pytest
from pathlib import Path

from framework.tools.overflow.local import LocalFileToolOverflowStore
from framework.tools.overflow.models import OverflowMetadata


@pytest.fixture
async def store(tmp_path: Path) -> LocalFileToolOverflowStore:
    s = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
    await s.initialize()
    return s


class TestStoreCreatesFiles:
    @pytest.mark.asyncio
    async def test_store_creates_files(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        ref = await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="a" * 120,
        )

        entry_dir = tmp_path / "tool_overflow" / "sess_1" / "call_1"
        assert entry_dir.exists()
        assert (entry_dir / ".meta.json").exists()
        assert (entry_dir / "1.full.txt").exists()
        assert (entry_dir / "1.summary.txt").exists()
        assert (entry_dir / "2.full.txt").exists()
        assert (entry_dir / "2.summary.txt").exists()
        assert (entry_dir / "3.full.txt").exists()
        assert (entry_dir / "3.summary.txt").exists()

        assert ref.chunk_count == 3
        assert ref.total_chars == 120


class TestReadChunk:
    @pytest.mark.asyncio
    async def test_read_chunk_returns_raw_content(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="x" * 120,
        )

        chunk = await store.read_chunk("sess_1", "call_1", 1)
        assert chunk is not None
        assert chunk == "x" * 50

    @pytest.mark.asyncio
    async def test_read_chunk_summary(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="y" * 120,
        )

        chunk = await store.read_chunk("sess_1", "call_1", 1, summary=True)
        assert chunk is not None
        assert chunk == "y" * 20

    @pytest.mark.asyncio
    async def test_read_chunk_not_found(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        result = await store.read_chunk("sess_1", "call_missing", 1)
        assert result is None


class TestReadMetadata:
    @pytest.mark.asyncio
    async def test_read_metadata_roundtrip(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="z" * 120,
        )

        meta = await store.read_metadata("sess_1", "call_1")
        assert meta is not None
        assert meta.tool_name == "read_file"
        assert meta.tool_call_id == "call_1"
        assert meta.session_id == "sess_1"
        assert meta.total_chars == 120
        assert meta.total_chunks == 3

    @pytest.mark.asyncio
    async def test_read_metadata_not_found(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        result = await store.read_metadata("sess_1", "call_missing")
        assert result is None


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_directory(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="a" * 120,
        )

        entry_dir = tmp_path / "tool_overflow" / "sess_1" / "call_1"
        assert entry_dir.exists()

        deleted = await store.delete("sess_1", "call_1")
        assert deleted is True
        assert not entry_dir.exists()

    @pytest.mark.asyncio
    async def test_delete_not_found(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        deleted = await store.delete("sess_1", "call_missing")
        assert deleted is False


class TestListToolCallIds:
    @pytest.mark.asyncio
    async def test_list_sorted_by_created_at(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="a" * 10,
        )
        await asyncio.sleep(0.05)
        await store.store(
            session_id="sess_1",
            tool_call_id="call_2",
            tool_name="read_file",
            content="b" * 10,
        )
        await asyncio.sleep(0.05)
        await store.store(
            session_id="sess_1",
            tool_call_id="call_3",
            tool_name="read_file",
            content="c" * 10,
        )

        ids = await store.list_tool_call_ids("sess_1")
        assert ids == ["call_1", "call_2", "call_3"]

    @pytest.mark.asyncio
    async def test_list_empty_session(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=50, summary_chars=20)
        await store.initialize()

        ids = await store.list_tool_call_ids("sess_empty")
        assert ids == []


class TestFullFileUnderChunkSize:
    @pytest.mark.asyncio
    async def test_full_file_content_within_chunk_size(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=100, summary_chars=20)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="x" * 250,
        )

        entry_dir = tmp_path / "tool_overflow" / "sess_1" / "call_1"
        for file_name in ["1.full.txt", "2.full.txt", "3.full.txt"]:
            content = (entry_dir / file_name).read_text(encoding="utf-8")
            assert len(content) <= 100, f"{file_name} content exceeds max_chunk_size"


class TestSummaryCharsBound:
    @pytest.mark.asyncio
    async def test_summary_chars_bound(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path, max_chunk_size=100, summary_chars=200)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="s" * 500,
        )

        entry_dir = tmp_path / "tool_overflow" / "sess_1" / "call_1"
        summary = (entry_dir / "1.summary.txt").read_text(encoding="utf-8")
        assert len(summary) <= 200
