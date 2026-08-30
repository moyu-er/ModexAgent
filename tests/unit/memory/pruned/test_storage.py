import json

import pytest

from modex_agent.memory.pruned.models import PrunedIndexEntry
from modex_agent.memory.pruned.storage import FilePrunedStorage


def _entry(**overrides: object) -> PrunedIndexEntry:
    """Build a PrunedIndexEntry with sensible defaults, overridden by *overrides*."""
    defaults = {
        "id": 1,
        "cleanup_time": 1717500000,
        "cleanup_time_display": "2024-06-04 12:00",
        "message_count": 5,
        "content_filename": "pruned_001.md",
    }
    defaults.update(overrides)
    return PrunedIndexEntry(**defaults)  # type: ignore[arg-type]


class TestFilePrunedStorage:

    def test_has_content_false_when_empty(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        assert storage.has_content() is False

    def test_has_content_false_when_only_index(self, tmp_path: pytest.TempPathFactory) -> None:
        """An index.jsonl file alone is not content — only .md transcripts count."""
        storage = FilePrunedStorage(tmp_path / "pruned")
        storage.save_index([])  # creates index.jsonl but no transcript files
        assert storage.has_content() is False

    def test_has_content_true_after_md_write(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        storage.write_transcript("pruned_001.md", "# Transcript")
        assert storage.has_content() is True

    def test_write_and_read_pruned_file(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        text = "# Transcript #1 · topic\n\n---\n\n## [001] user · 08-19 10:31\n\nhello\n\n---\n"
        storage.write_transcript("pruned_001.md", text)
        filepath = tmp_path / "pruned" / "pruned_001.md"
        assert filepath.exists()
        assert filepath.read_text(encoding="utf-8") == text

    def test_append_and_read_index(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        entry_a = _entry(id=1, content_filename="pruned_001.md")
        entry_b = _entry(id=2, content_filename="pruned_002.md")
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
            entry = _entry(id=i, content_filename=f"pruned_00{i}.md")
            storage.append_index(entry)
            storage.write_transcript(f"pruned_00{i}.md", "text")
        storage.prune_oldest(keep_count=2)
        entries = storage.read_index()
        assert len(entries) == 2
        assert entries[0].id == 2
        assert entries[1].id == 3

    def test_prune_oldest_deletes_files(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        for i in range(1, 4):
            storage.write_transcript(f"pruned_00{i}.md", "text")
            storage.append_index(_entry(id=i, content_filename=f"pruned_00{i}.md"))
        storage.prune_oldest(keep_count=2)
        assert not (tmp_path / "pruned" / "pruned_001.md").exists()
        assert (tmp_path / "pruned" / "pruned_002.md").exists()
        assert (tmp_path / "pruned" / "pruned_003.md").exists()

    def test_prune_oldest_noop_when_under_limit(self, tmp_path: pytest.TempPathFactory) -> None:
        storage = FilePrunedStorage(tmp_path / "pruned")
        storage.write_transcript("pruned_001.md", "text")
        storage.append_index(_entry(id=1, content_filename="pruned_001.md"))
        storage.prune_oldest(keep_count=5)
        assert len(storage.read_index()) == 1
        assert (tmp_path / "pruned" / "pruned_001.md").exists()

    def test_creates_directory_on_first_write(self, tmp_path: pytest.TempPathFactory) -> None:
        pruned_dir = tmp_path / "does_not_exist"
        storage = FilePrunedStorage(pruned_dir)
        assert not pruned_dir.exists()
        storage.write_transcript("pruned_001.md", "text")
        assert pruned_dir.exists()


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
