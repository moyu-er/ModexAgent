"""Tests for framework.workspace.context.WorkspaceContext — workspace identity."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from framework.workspace.context import WorkspaceContext


def test_from_target_builds_paths_and_home_flag(tmp_path: Path) -> None:
    home = tmp_path / "proj"
    home.mkdir()
    target = tmp_path / "wsB"
    target.mkdir()
    ctx = WorkspaceContext.from_target(target, data_dir_name=".modex", home=home)
    assert ctx.target == target.resolve()
    assert ctx.is_home is False
    assert ctx.paths.root == (target / ".modex").resolve()


def test_home_target_is_home(tmp_path: Path) -> None:
    home = tmp_path / "proj"
    home.mkdir()
    ctx = WorkspaceContext.from_target(home, data_dir_name=".modex", home=home)
    assert ctx.is_home is True
    assert ctx.target == home.resolve()


def test_context_is_frozen(tmp_path: Path) -> None:
    ctx = WorkspaceContext.from_target(tmp_path, data_dir_name=".modex", home=tmp_path)
    assert dataclasses.is_dataclass(ctx)
    raised = False
    try:
        ctx.target = Path("/x")  # type: ignore[misc]
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised
