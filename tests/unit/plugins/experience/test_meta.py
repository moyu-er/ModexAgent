"""Tests for PerFileExperienceMetaStore — no cache, direct disk I/O."""
from pathlib import Path

from modex_agent.plugins.defaults.capabilities.experience.metadata import (
    ExperienceMetaRecord,
    PerFileExperienceMetaStore,
)


def _make_exp_dir(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_set_then_get(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    _make_exp_dir(tmp_path, "test-exp")
    record = ExperienceMetaRecord(use_count=3, view_count=5, pinned=True)
    store.set("test-exp", record)

    got = store.get("test-exp")
    assert got is not None
    assert got.use_count == 3
    assert got.view_count == 5
    assert got.pinned is True


def test_set_writes_to_disk(tmp_path: Path) -> None:
    """Verify the file is on disk (no cache)."""
    store = PerFileExperienceMetaStore(tmp_path)
    _make_exp_dir(tmp_path, "test-exp")
    store.set("test-exp", ExperienceMetaRecord(use_count=7))

    meta_file = tmp_path / "test-exp" / ".exp.meta.json"
    assert meta_file.exists()
    import json

    data = json.loads(meta_file.read_text())
    assert data["use_count"] == 7


def test_get_nonexistent_returns_none(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    assert store.get("no-such-exp") is None


def test_remove_deletes_file(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    _make_exp_dir(tmp_path, "test-exp")
    store.set("test-exp", ExperienceMetaRecord())
    meta_file = tmp_path / "test-exp" / ".exp.meta.json"
    assert meta_file.exists()

    store.remove("test-exp")
    assert not meta_file.exists()


def test_remove_nonexistent_is_noop(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    store.remove("no-such-exp")  # should not raise


def test_migrate_moves_record(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    _make_exp_dir(tmp_path, "old-name")
    _make_exp_dir(tmp_path, "new-name")
    store.set("old-name", ExperienceMetaRecord(use_count=10, view_count=2))

    store.migrate("old-name", "new-name")

    assert store.get("old-name") is None
    got = store.get("new-name")
    assert got is not None
    assert got.use_count == 10
    assert got.view_count == 2


def test_migrate_nonexistent_is_noop(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    store.migrate("no-old", "no-new")  # should not raise


def test_list_all(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    for name in ("exp-a", "exp-b", "exp-c"):
        _make_exp_dir(tmp_path, name)
    store.set("exp-a", ExperienceMetaRecord(use_count=1))
    store.set("exp-b", ExperienceMetaRecord(use_count=2))
    # exp-c has no meta file

    all_records = store.list_all()
    assert len(all_records) == 2
    assert all_records["exp-a"].use_count == 1
    assert all_records["exp-b"].use_count == 2


def test_list_all_empty_dir(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    assert store.list_all() == {}


def test_bump_use_increments(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    _make_exp_dir(tmp_path, "test-exp")
    store.set("test-exp", ExperienceMetaRecord(use_count=3))

    result = store.bump_use("test-exp")
    assert result.use_count == 4
    assert result.last_used_at is not None

    # Verify on disk
    got = store.get("test-exp")
    assert got is not None
    assert got.use_count == 4


def test_bump_use_creates_new_record(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    _make_exp_dir(tmp_path, "new-exp")
    result = store.bump_use("new-exp")
    assert result.use_count == 1
    assert result.created_at is not None


def test_bump_view_increments(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    _make_exp_dir(tmp_path, "test-exp")
    store.set("test-exp", ExperienceMetaRecord(view_count=2))

    result = store.bump_view("test-exp")
    assert result.view_count == 3


def test_touch_updates_timestamp_without_counters(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    _make_exp_dir(tmp_path, "test-exp")
    store.set("test-exp", ExperienceMetaRecord(use_count=5, view_count=3))

    result = store.touch("test-exp")
    assert result.use_count == 5
    assert result.view_count == 3
    assert result.last_used_at is not None


def test_corrupt_file_returns_none(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    exp_dir = _make_exp_dir(tmp_path, "bad-exp")
    (exp_dir / ".exp.meta.json").write_text("not json{{{", encoding="utf-8")

    assert store.get("bad-exp") is None


def test_non_dict_json_returns_none(tmp_path: Path) -> None:
    store = PerFileExperienceMetaStore(tmp_path)
    exp_dir = _make_exp_dir(tmp_path, "bad-exp")
    (exp_dir / ".exp.meta.json").write_text("[1, 2, 3]", encoding="utf-8")

    assert store.get("bad-exp") is None


def test_callable_root_workspace_switch(tmp_path: Path) -> None:
    """Callable root allows dynamic path resolution (workspace switch)."""
    ws_a = tmp_path / "workspace-a"
    ws_b = tmp_path / "workspace-b"
    ws_a.mkdir()
    ws_b.mkdir()

    current = [ws_a]  # mutable container for closure
    store = PerFileExperienceMetaStore(lambda: current[0])

    _make_exp_dir(ws_a, "exp-1")
    _make_exp_dir(ws_b, "exp-2")

    store.set("exp-1", ExperienceMetaRecord(use_count=1))
    assert store.get("exp-1") is not None

    # Simulate workspace switch
    current[0] = ws_b
    assert store.get("exp-1") is None  # old workspace data not visible
    assert store.list_all() == {}  # new workspace is empty

    store.set("exp-2", ExperienceMetaRecord(use_count=2))
    assert store.get("exp-2") is not None
