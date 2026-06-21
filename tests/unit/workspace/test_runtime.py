"""Tests for framework.workspace.runtime contextvar (bind/resolve)."""

from __future__ import annotations

from pathlib import Path

from framework.workspace.runtime import bind_workspace_root, resolve_workspace_root


def test_resolve_defaults_to_cwd_when_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace_root() == tmp_path


def test_bind_sets_and_resets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace_root() == tmp_path  # unset → cwd
    with bind_workspace_root(Path("/opt/ws")):
        assert resolve_workspace_root() == Path("/opt/ws")
    assert resolve_workspace_root() == tmp_path  # reset → cwd again
