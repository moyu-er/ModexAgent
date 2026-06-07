import json
from pathlib import Path

from framework.core.experience.usage import ExperienceUsageTracker


def test_bump_use_creates_entry(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    tracker = ExperienceUsageTracker(usage_file)
    tracker.bump_use("test-experience")
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert data["test-experience"]["use_count"] == 1


def test_bump_use_increments(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    usage_file.write_text(json.dumps({"test-experience": {"use_count": 3}}))
    tracker = ExperienceUsageTracker(usage_file)
    tracker.bump_use("test-experience")
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert data["test-experience"]["use_count"] == 4


def test_bump_view_defaults(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    tracker = ExperienceUsageTracker(usage_file)
    tracker.bump_view("test-experience")
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert data["test-experience"]["view_count"] == 1


def test_update_timestamp_sets_created_at(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    tracker = ExperienceUsageTracker(usage_file)
    tracker.update_timestamp("test-experience")
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert "created_at" in data["test-experience"]
    assert "last_used_at" in data["test-experience"]


def test_get_record_nonexistent(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    tracker = ExperienceUsageTracker(usage_file)
    assert tracker.get_record("nonexistent") is None


def test_no_file_does_not_crash(tmp_path: Path) -> None:
    usage_file = tmp_path / "nonexistent" / ".usage.json"
    tracker = ExperienceUsageTracker(usage_file)
    tracker.bump_use("test")  # should not raise
    assert tracker.get_record("test") == {"use_count": 1}


def test_bump_view_increments_existing(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    usage_file.write_text(json.dumps({"test-experience": {"view_count": 5}}))
    tracker = ExperienceUsageTracker(usage_file)
    tracker.bump_view("test-experience")
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert data["test-experience"]["view_count"] == 6


def test_corrupt_file_returns_empty(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    usage_file.write_text("not json")
    tracker = ExperienceUsageTracker(usage_file)
    assert tracker.get_all_records() == {}
    # mutation should still work after encountering corrupt file
    tracker.bump_use("test-experience")
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert data["test-experience"]["use_count"] == 1


def test_non_dict_json_returns_empty(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    usage_file.write_text(json.dumps([1, 2, 3]))
    tracker = ExperienceUsageTracker(usage_file)
    assert tracker.get_all_records() == {}


def test_migrate_record(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    usage_file.write_text(json.dumps({
        "old-name": {"use_count": 5, "last_used_at": "2024-01-01T00:00:00+00:00"},
        "keep-me": {"use_count": 3},
    }))
    tracker = ExperienceUsageTracker(usage_file)
    tracker.migrate_record("old-name", "new-name")
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert "old-name" not in data
    assert data["new-name"]["use_count"] == 5
    assert data["new-name"]["last_used_at"] == "2024-01-01T00:00:00+00:00"
    assert "keep-me" in data


def test_migrate_record_nonexistent_is_noop(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    tracker = ExperienceUsageTracker(usage_file)
    tracker.migrate_record("nonexistent", "new-name")  # should not raise


def test_remove_record_deletes_entry(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    usage_file.write_text(json.dumps({
        "exp-a": {"use_count": 1},
        "exp-b": {"use_count": 2},
    }))
    tracker = ExperienceUsageTracker(usage_file)
    tracker.remove_record("exp-a")
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert "exp-a" not in data
    assert "exp-b" in data
    assert data["exp-b"]["use_count"] == 2


def test_remove_record_nonexistent_is_noop(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    usage_file.write_text(json.dumps({"exp-a": {"use_count": 1}}))
    tracker = ExperienceUsageTracker(usage_file)
    tracker.remove_record("nonexistent")  # should not raise
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert "exp-a" in data


def test_update_timestamp_to_sets_from_mtime(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    tracker = ExperienceUsageTracker(usage_file)
    # 1700000000.0 = 2023-11-14T22:13:20+00:00
    tracker.update_timestamp_to("test-exp", 1700000000.0)
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert "2023-11-14" in data["test-exp"]["last_used_at"]
    assert "2023-11-14" in data["test-exp"]["created_at"]


def test_update_timestamp_to_preserves_created_at(tmp_path: Path) -> None:
    usage_file = tmp_path / ".usage.json"
    usage_file.write_text(json.dumps({
        "test-exp": {"created_at": "2020-01-01T00:00:00+00:00", "use_count": 1},
    }))
    tracker = ExperienceUsageTracker(usage_file)
    tracker.update_timestamp_to("test-exp", 1700000000.0)
    tracker.flush()
    data = json.loads(usage_file.read_text())
    assert data["test-exp"]["created_at"] == "2020-01-01T00:00:00+00:00"
    assert "2023-11-14" in data["test-exp"]["last_used_at"]
