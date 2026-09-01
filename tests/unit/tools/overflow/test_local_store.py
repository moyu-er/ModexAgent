from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from modex_agent.tools.overflow.local import LocalFileToolOverflowStore
from modex_agent.tools.overflow.models import OverflowMetadata, OverflowRef


@pytest.fixture
async def store(tmp_path: Path) -> LocalFileToolOverflowStore:
    s = LocalFileToolOverflowStore(workspace=tmp_path)
    await s.initialize()
    return s


class TestStoreCreatesFiles:
    @pytest.mark.asyncio
    async def test_store_creates_files(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()

        ref = await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="a" * 120,
        )

        entry_dir = tmp_path / "tool_overflow" / "sess_1" / "call_1"
        assert entry_dir.exists()
        assert {path.name for path in entry_dir.iterdir()} == {".meta.json", "full.txt"}
        assert (entry_dir / "full.txt").read_text(encoding="utf-8") == "a" * 120
        assert ref.total_chars == 120
        assert ref.dir_path == str(entry_dir.resolve())
        assert ref.metadata_path == str((entry_dir / ".meta.json").resolve())
        assert set(OverflowRef.model_fields) == {
            "dir_path",
            "total_chars",
            "metadata_path",
        }


class TestReadMetadata:
    @pytest.mark.asyncio
    async def test_read_metadata_roundtrip(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
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
        assert set(OverflowMetadata.model_fields) == {
            "tool_name",
            "tool_call_id",
            "session_id",
            "created_at",
            "total_chars",
        }

    @pytest.mark.asyncio
    async def test_meta_json_is_written_after_full_txt(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="a" * 120,
        )

        entry_dir = tmp_path / "tool_overflow" / "sess_1" / "call_1"
        full_stat = (entry_dir / "full.txt").stat()
        meta_stat = (entry_dir / ".meta.json").stat()
        # .meta.json is the commit marker — it must land on disk last.
        assert meta_stat.st_mtime_ns >= full_stat.st_mtime_ns

    @pytest.mark.asyncio
    async def test_read_metadata_not_found(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()

        result = await store.read_metadata("sess_1", "call_missing")
        assert result is None


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_directory(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
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
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()

        deleted = await store.delete("sess_1", "call_missing")
        assert deleted is False


class TestListToolCallIds:
    @pytest.mark.asyncio
    async def test_list_sorted_by_created_at(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
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
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()

        ids = await store.list_tool_call_ids("sess_empty")
        assert ids == []


class TestFullFile:
    @pytest.mark.asyncio
    async def test_full_file_contains_complete_content(self, tmp_path: Path) -> None:
        store = LocalFileToolOverflowStore(workspace=tmp_path)
        await store.initialize()

        await store.store(
            session_id="sess_1",
            tool_call_id="call_1",
            tool_name="read_file",
            content="x" * 250,
        )

        entry_dir = tmp_path / "tool_overflow" / "sess_1" / "call_1"
        assert (entry_dir / "full.txt").read_text(encoding="utf-8") == "x" * 250
