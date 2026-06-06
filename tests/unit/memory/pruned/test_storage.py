import json

import pytest

from framework.memory.pruned.models import PrunedIndexEntry
from framework.memory.pruned.storage import FilePrunedStorage


def _entry(**overrides: object) -> PrunedIndexEntry:
    """Build a PrunedIndexEntry with sensible defaults, overridden by *overrides*."""
    defaults = {
        "id": 1,
        "cleanup_time": 1717500000,
        "cleanup_time_display": "2024-06-04 12:00",
        "message_count": 5,
        "content_filename": "pruned_001.jsonl",
    }
    defaults.update(overrides)
    return PrunedIndexEntry(**defaults)  # type: ignore[arg-type]


class TestFilePrunedStorage:

    def test_has_content_false_when_empty(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        assert storage.has_content() is False

    def test_has_content_true_when_index_has_entries(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        entry = _entry()
        storage.append_index(entry)
        # Index entries count as content (supports MD-archive-based pruned)
        assert storage.has_content() is True

    def test_has_content_true_when_index_file_exists(self, tmp_path) -> None:
        """An index.jsonl file (even empty) counts as content — get_injection_xml
        handles the empty-valid-entries case internally."""
        storage = FilePrunedStorage(tmp_path / "pruned")
        storage.save_index([])  # creates index.jsonl but with no entries
        assert storage.has_content() is True

    def test_has_content_true_after_write(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        storage.write_pruned("pruned_001.jsonl", [{"role": "user", "content": "hi"}])
        assert storage.has_content() is True

    def test_write_and_read_pruned_file(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "world"},
        ]
        storage.write_pruned("pruned_001.jsonl", messages)
        filepath = tmp_path / "pruned" / "pruned_001.jsonl"
        assert filepath.exists()
        lines = filepath.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == messages[0]
        assert json.loads(lines[1]) == messages[1]

    def test_append_and_read_index(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        entry_a = _entry(id=1, content_filename="pruned_001.jsonl")
        entry_b = _entry(id=2, content_filename="pruned_002.jsonl")
        storage.append_index(entry_a)
        storage.append_index(entry_b)
        entries = storage.read_index()
        assert len(entries) == 2
        assert entries[0] == entry_a
        assert entries[1] == entry_b

    def test_read_index_empty(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        assert storage.read_index() == []

    def test_get_directory_path_is_absolute(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        path = storage.get_directory_path()
        assert path == str((tmp_path / "pruned").resolve())
        # Absolute path check (works on all platforms)
        import os
        assert os.path.isabs(path)

    def test_prune_oldest_keeps_recent_entries(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        for i in range(1, 4):
            entry = _entry(id=i, content_filename=f"pruned_00{i}.jsonl")
            storage.append_index(entry)
            storage.write_pruned(f"pruned_00{i}.jsonl", [{"role": "user", "content": f"msg{i}"}])
        storage.prune_oldest(keep_count=2)
        entries = storage.read_index()
        assert len(entries) == 2
        assert entries[0].id == 2
        assert entries[1].id == 3

    def test_prune_oldest_deletes_files(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        for i in range(1, 4):
            storage.write_pruned(f"pruned_00{i}.jsonl", [{"role": "user"}])
            storage.append_index(_entry(id=i, content_filename=f"pruned_00{i}.jsonl"))
        storage.prune_oldest(keep_count=2)
        assert not (tmp_path / "pruned" / "pruned_001.jsonl").exists()
        assert (tmp_path / "pruned" / "pruned_002.jsonl").exists()
        assert (tmp_path / "pruned" / "pruned_003.jsonl").exists()

    def test_prune_oldest_noop_when_under_limit(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        storage.write_pruned("pruned_001.jsonl", [{"role": "user"}])
        storage.append_index(_entry(id=1, content_filename="pruned_001.jsonl"))
        storage.prune_oldest(keep_count=5)
        assert len(storage.read_index()) == 1
        assert (tmp_path / "pruned" / "pruned_001.jsonl").exists()

    def test_creates_directory_on_first_write(self, tmp_path: pytest.TempPathFactory) -> None:
        pruned_dir = tmp_path / "does_not_exist"
        storage = FilePrunedStorage(pruned_dir)
        assert not pruned_dir.exists()
        storage.write_pruned("pruned_001.jsonl", [{"role": "user", "content": "hi"}])
        assert pruned_dir.exists()

    def test_write_pruned_with_empty_messages(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        storage.write_pruned("pruned_empty.jsonl", [])
        filepath = tmp_path / "pruned" / "pruned_empty.jsonl"
        assert filepath.exists()
        content = filepath.read_text(encoding="utf-8").strip()
        assert content == ""


class TestIndexResilience:
    """read_index must survive malformed lines caused by LLM editing index.jsonl.

    The system prompt tells agents that index.jsonl is editable. If an agent
    corrupts a line (invalid JSON, missing required fields), the remaining
    valid entries must still be returned — not the entire injection failing.
    """

    def test_skips_malformed_json_line(self, tmp_path) -> None:
        """A line that is not valid JSON should be skipped, not crash."""
        storage = FilePrunedStorage(tmp_path / "pruned")
        entry_good = _entry(id=1, content_filename="a.jsonl")
        storage.append_index(entry_good)

        # Manually append a corrupted line (simulating agent edit mistake)
        index_path = tmp_path / "pruned" / "index.jsonl"
        with open(index_path, "a", encoding="utf-8") as fh:
            fh.write("this is not json at all\n")

        # Should return only the valid entry
        entries = storage.read_index()
        assert len(entries) == 1
        assert entries[0].id == 1

    def test_skips_line_missing_required_fields(self, tmp_path) -> None:
        """A JSON line missing required fields (e.g. 'id') should be skipped."""
        storage = FilePrunedStorage(tmp_path / "pruned")
        entry_good = _entry(id=1, content_filename="a.jsonl")
        storage.append_index(entry_good)

        # Append a JSON object that lacks required fields
        index_path = tmp_path / "pruned" / "index.jsonl"
        with open(index_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"topic": "missing id and other fields"}) + "\n")

        entries = storage.read_index()
        assert len(entries) == 1
        assert entries[0].id == 1

    def test_returns_valid_entries_amidst_multiple_bad(self, tmp_path) -> None:
        """Multiple corrupted lines between valid ones should all be skipped."""
        storage = FilePrunedStorage(tmp_path / "pruned")
        entry1 = _entry(id=1, content_filename="a.jsonl")
        entry2 = _entry(id=2, content_filename="b.jsonl")
        storage.append_index(entry1)
        storage.append_index(entry2)

        # Manually write a mixed file: valid, bad-json, valid, incomplete-json
        index_path = tmp_path / "pruned" / "index.jsonl"
        lines = [
            json.dumps(entry1.to_dict()),
            "corrupted line {{{",
            json.dumps(entry2.to_dict()),
            json.dumps({"topic": "no id"}),
        ]
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        entries = storage.read_index()
        assert len(entries) == 2
        assert entries[0].id == 1
        assert entries[1].id == 2
