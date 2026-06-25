"""Tests for framework.workspace.store.GlobalWorkspaceStore (file-backed)."""

from __future__ import annotations

import json
from pathlib import Path

from modex_agent.workspace.store import GlobalWorkspaceStore


def test_roundtrip_targets(tmp_path: Path) -> None:
    s = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
    s.save_known_targets([tmp_path / "a", tmp_path / "b"])
    loaded = {t.resolve() for t in s.load_known_targets()}
    assert loaded == {(tmp_path / "a").resolve(), (tmp_path / "b").resolve()}


def test_persists_under_registry_dir(tmp_path: Path) -> None:
    s = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
    s.save_known_targets([tmp_path / "a"])
    f = tmp_path / ".modex" / "_registry" / "workspaces.json"
    assert f.exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert str((tmp_path / "a").resolve()) in data["targets"]


def test_load_when_absent_returns_empty(tmp_path: Path) -> None:
    s = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
    assert s.load_known_targets() == []
