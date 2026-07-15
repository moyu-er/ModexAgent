"""Tests for workspace.enabled flag — disabled controller rejects open_workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.store import GlobalWorkspaceStore
from modex_agent.workspace.models import CdError

from ._stubs import StubFactory


@pytest.fixture
def disabled_controller(tmp_path: Path) -> WorkspaceController:
    home = tmp_path / "proj"
    home.mkdir()
    reg = WorkspaceRegistry(
        home=home, data_dir_name=".modex",
        factory=StubFactory(), store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
    )
    return WorkspaceController(
        registry=reg, data_dir_name=".modex", enabled=False
    )


@pytest.fixture
def enabled_controller(tmp_path: Path) -> WorkspaceController:
    home = tmp_path / "proj"
    home.mkdir()
    reg = WorkspaceRegistry(
        home=home, data_dir_name=".modex",
        factory=StubFactory(), store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
    )
    return WorkspaceController(
        registry=reg, data_dir_name=".modex", enabled=True
    )


async def test_disabled_open_workspace_rejects(
    disabled_controller: WorkspaceController, tmp_path: Path
) -> None:
    target = tmp_path / "wsB"
    target.mkdir()
    res = await disabled_controller.open_workspace(str(target))
    assert not res.success
    assert res.error == CdError.INVALID_PATH
    assert "disabled" in res.notice


async def test_enabled_open_workspace_works(
    enabled_controller: WorkspaceController, tmp_path: Path
) -> None:
    target = tmp_path / "wsB"
    target.mkdir()
    res = await enabled_controller.open_workspace(str(target))
    assert res.success
    assert res.current_path == target.resolve()
