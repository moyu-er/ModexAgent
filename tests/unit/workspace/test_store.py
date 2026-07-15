"""Tests for framework.workspace.store.GlobalWorkspaceStore (file-backed)."""

from __future__ import annotations

import json
from pathlib import Path

from modex_agent.workspace.store import GlobalWorkspaceStore


async def test_roundtrip_targets(tmp_path: Path) -> None:
    s = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
    await s.save_known_targets([tmp_path / "a", tmp_path / "b"])
    loaded = {t.resolve() for t in await s.load_known_targets()}
    assert loaded == {(tmp_path / "a").resolve(), (tmp_path / "b").resolve()}


async def test_persists_under_registry_dir(tmp_path: Path) -> None:
    s = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
    await s.save_known_targets([tmp_path / "a"])
    f = tmp_path / ".modex" / "_registry" / "workspaces.json"
    assert f.exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    assert "workspaces" in data
    assert len(data["workspaces"]) == 1
    entry = data["workspaces"][0]
    assert entry["target_path"] == str((tmp_path / "a").resolve())
    assert entry["is_home"] is False


async def test_load_when_absent_returns_empty(tmp_path: Path) -> None:
    s = GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex")
    assert await s.load_known_targets() == []
