"""Tests for framework.workspace.control.WorkspaceController (stub registry)."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.store import GlobalWorkspaceStore
from modex_agent.workspace.models import CdError

from ._stubs import StubFactory


@pytest.fixture
def controller(tmp_path: Path) -> WorkspaceController:
    home = tmp_path / "proj"
    home.mkdir()
    reg = WorkspaceRegistry(
        home=home, data_dir_name=".modex",
        factory=StubFactory(), store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
    )
    return WorkspaceController(
        registry=reg, data_dir_name=".modex"
    )


async def test_open_workspace_valid_target_registers(
    controller: WorkspaceController, tmp_path: Path
) -> None:
    target = tmp_path / "wsB"
    target.mkdir()
    res = await controller.open_workspace(str(target))
    assert res.success
    assert res.current_path == target.resolve()


async def test_open_workspace_nonexistent_path_fails(
    controller: WorkspaceController, tmp_path: Path
) -> None:
    res = await controller.open_workspace(str(tmp_path / "nope"))
    assert not res.success
    assert res.error == CdError.PATH_NOT_FOUND


async def test_open_workspace_not_a_directory_fails(
    controller: WorkspaceController, tmp_path: Path
) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    res = await controller.open_workspace(str(f))
    assert not res.success
    assert res.error == CdError.NOT_A_DIRECTORY


def test_home_returns_registry_home(controller: WorkspaceController, tmp_path: Path) -> None:
    assert controller.home == (tmp_path / "proj").resolve()
