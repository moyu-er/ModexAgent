"""Tests for DirArchiveStorage."""
from __future__ import annotations

import pytest

from framework.memory.stores.dir_archive import DirArchiveStorage


@pytest.fixture
def store(tmp_path: pytest.TempPathFactory) -> DirArchiveStorage:
    return DirArchiveStorage(tmp_path / "archives")


@pytest.mark.asyncio
class TestDirArchiveStorage:
    async def test_read_state_default(self, store: DirArchiveStorage) -> None:
        result = await store.read_archive_state()
        assert result is None

    async def test_write_and_read_state(self, store: DirArchiveStorage) -> None:
        state = {"next_archive_id": 5, "knowledge_consumed_archive_id": 3}
        await store.write_archive_state(state)
        loaded = await store.read_archive_state()
        assert loaded == state

    async def test_append_channel_log_creates_dir(
        self, store: DirArchiveStorage
    ) -> None:
        await store.write_archive_state({"next_archive_id": 1})
        await store.append_channel_log(
            "context", {"summary": "hello world"}
        )
        assert (store.base_dir / "1").is_dir()
        assert (store.base_dir / "1" / "context.md").read_text("utf-8") == "hello world"

    async def test_append_returns_record_with_archive_id(
        self, store: DirArchiveStorage
    ) -> None:
        await store.write_archive_state({"next_archive_id": 7})
        record = await store.append_channel_log(
            "knowledge", {"summary": "fact"}
        )
        assert record["archive_id"] == 7
        assert record["channel"] == "knowledge"
        assert record["summary"] == "fact"

    async def test_read_channel_logs_since_id(
        self, store: DirArchiveStorage
    ) -> None:
        # Create three archive dirs with context.md
        for aid, text in [(1, "first"), (2, "second"), (3, "third")]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text(text, encoding="utf-8")

        logs = await store.read_channel_logs("context", since_archive_id=1)
        ids = [entry["archive_id"] for entry in logs]
        assert ids == [2, 3]
        assert logs[0]["summary"] == "second"
        assert logs[1]["summary"] == "third"

    async def test_write_and_read_archive_md_files(
        self, store: DirArchiveStorage
    ) -> None:
        await store.write_archive_file(1, "context.md", "# Context")
        content = await store.read_archive_file(1, "context.md")
        assert content == "# Context"

    async def test_read_nonexistent_archive_file(
        self, store: DirArchiveStorage
    ) -> None:
        result = await store.read_archive_file(99, "context.md")
        assert result is None

    async def test_list_archives_descending(self, store: DirArchiveStorage) -> None:
        for aid in [1, 3, 5]:
            (store.base_dir / str(aid)).mkdir(parents=True, exist_ok=True)
        ids = await store.list_archives()
        assert ids == [5, 3, 1]

    async def test_list_archives_since_id(self, store: DirArchiveStorage) -> None:
        for aid in [1, 2, 3, 4, 5]:
            (store.base_dir / str(aid)).mkdir(parents=True, exist_ok=True)
        ids = await store.list_archives(since_id=3)
        assert ids == [5, 4]

    async def test_is_archive_complete_all_files(
        self, store: DirArchiveStorage
    ) -> None:
        d = store.base_dir / "1"
        d.mkdir(parents=True, exist_ok=True)
        for name in ("context.md", "knowledge.md", "index.md"):
            (d / name).write_text("content", encoding="utf-8")
        assert await store.is_archive_complete(1) is True

    async def test_is_archive_complete_missing_file(
        self, store: DirArchiveStorage
    ) -> None:
        d = store.base_dir / "2"
        d.mkdir(parents=True, exist_ok=True)
        (d / "context.md").write_text("content", encoding="utf-8")
        (d / "knowledge.md").write_text("content", encoding="utf-8")
        # index.md is missing
        assert await store.is_archive_complete(2) is False

    async def test_is_archive_complete_empty_file(
        self, store: DirArchiveStorage
    ) -> None:
        d = store.base_dir / "3"
        d.mkdir(parents=True, exist_ok=True)
        (d / "context.md").write_text("content", encoding="utf-8")
        (d / "knowledge.md").write_text("content", encoding="utf-8")
        (d / "index.md").write_text("", encoding="utf-8")  # 0 bytes
        assert await store.is_archive_complete(3) is False

    async def test_is_archive_complete_no_dir(
        self, store: DirArchiveStorage
    ) -> None:
        assert await store.is_archive_complete(999) is False

    async def test_directory_property_alias(
        self, store: DirArchiveStorage
    ) -> None:
        assert store.directory == store.base_dir

    async def test_read_channel_logs_no_base_dir(
        self, store: DirArchiveStorage
    ) -> None:
        result = await store.read_channel_logs("context")
        assert result == []

    async def test_list_archives_no_base_dir(
        self, store: DirArchiveStorage
    ) -> None:
        result = await store.list_archives()
        assert result == []

    async def test_read_state_invalid_json(
        self, store: DirArchiveStorage
    ) -> None:
        store.base_dir.mkdir(parents=True, exist_ok=True)
        (store.base_dir / "state.json").write_text("not json", encoding="utf-8")
        result = await store.read_archive_state()
        # Corrupted state is backed up and empty dict returned (so callers
        # get a fresh start instead of silently losing state).
        assert result == {}
        assert (store.base_dir / "state.json.bak").exists()

    async def test_save_channel_logs_deletes_missing_dirs(
        self, store: DirArchiveStorage
    ) -> None:
        for aid in [1, 2, 3]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            for name in ("context.md", "knowledge.md", "index.md"):
                (d / name).write_text("content", encoding="utf-8")

        await store.save_channel_logs("context", [
            {"archive_id": 2},
            {"archive_id": 3},
        ])

        assert not (store.base_dir / "1").exists()
        assert (store.base_dir / "2").exists()
        assert (store.base_dir / "3").exists()

    async def test_prune_to_max_deletes_oldest(self, store: DirArchiveStorage) -> None:
        for aid in [1, 2, 3, 4, 5]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text(f"entry {aid}", encoding="utf-8")

        # min_safe_id=5 means all archives are consumed and eligible for deletion
        deleted = await store.prune_to_max(3, min_safe_id=5)

        assert deleted == 2
        assert not (store.base_dir / "1").exists()
        assert not (store.base_dir / "2").exists()
        assert (store.base_dir / "3").exists()
        assert (store.base_dir / "4").exists()
        assert (store.base_dir / "5").exists()

    async def test_prune_to_max_respects_min_safe_id(self, store: DirArchiveStorage) -> None:
        for aid in [1, 2, 3, 4, 5]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text(f"entry {aid}", encoding="utf-8")

        deleted = await store.prune_to_max(2, min_safe_id=3)

        assert deleted == 1
        assert not (store.base_dir / "1").exists()
        assert (store.base_dir / "2").exists()
        assert (store.base_dir / "3").exists()
        assert (store.base_dir / "4").exists()
        assert (store.base_dir / "5").exists()

    async def test_prune_to_max_noop_when_under_limit(
        self, store: DirArchiveStorage
    ) -> None:
        for aid in [1, 2]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text("content", encoding="utf-8")

        deleted = await store.prune_to_max(5)
        assert deleted == 0
        assert (store.base_dir / "1").exists()

    async def test_cleanup_empty_dirs_removes_empty(
        self, store: DirArchiveStorage
    ) -> None:
        empty = store.base_dir / "1"
        empty.mkdir(parents=True, exist_ok=True)

        almost_empty = store.base_dir / "2"
        almost_empty.mkdir(parents=True, exist_ok=True)
        (almost_empty / "context.md").write_text("", encoding="utf-8")
        (almost_empty / "knowledge.md").write_text("", encoding="utf-8")

        valid = store.base_dir / "3"
        valid.mkdir(parents=True, exist_ok=True)
        (valid / "context.md").write_text("has content", encoding="utf-8")

        count = await store.cleanup_empty_dirs()

        assert count == 2
        assert not empty.exists()
        assert not almost_empty.exists()
        assert valid.exists()

    async def test_append_skips_empty_summary(
        self, store: DirArchiveStorage
    ) -> None:
        await store.write_archive_state({"next_archive_id": 1})
        record = await store.append_channel_log(
            "context", {"summary": ""}
        )
        assert record["archive_id"] == 1
        # No .md file created for empty summary
        assert not (store.base_dir / "1" / "context.md").exists()

    async def test_append_defaults_next_id_without_state(
        self, store: DirArchiveStorage
    ) -> None:
        record = await store.append_channel_log(
            "context", {"summary": "auto"}
        )
        assert record["archive_id"] == 1

    async def test_read_channel_logs_limit(
        self, store: DirArchiveStorage
    ) -> None:
        for aid in [1, 2, 3, 4]:
            d = store.base_dir / str(aid)
            d.mkdir(parents=True, exist_ok=True)
            (d / "context.md").write_text(f"entry-{aid}", encoding="utf-8")

        logs = await store.read_channel_logs("context", limit=2)
        assert len(logs) == 2
        assert logs[0]["archive_id"] == 1
        assert logs[1]["archive_id"] == 2

    async def test_write_archive_file_returns_byte_count(
        self, store: DirArchiveStorage
    ) -> None:
        size = await store.write_archive_file(1, "context.md", "hello")
        assert size == len("hello".encode("utf-8"))
