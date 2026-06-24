"""Feedback loop for workspace switching disabled bug.

Reproduces the user-facing symptom: with the checked-in bot_config.yml,
workspace switching is disabled so POST /api/workspace/cd returns
``success: false`` with ``workspace switching disabled``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.registry import WorkspaceRegistry
from modex_agent.workspace.store import GlobalWorkspaceStore
from modex_agent.ioc.configs.app import AppConfig


def _real_project_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


@pytest.mark.asyncio
async def test_checked_in_config_enables_workspace_switching() -> None:
    """The checked-in bot_config.yml must enable workspace switching.

    Regression: WorkspaceConfig.enabled defaults to False. If bot_config.yml
    does not explicitly set ``workspace.enabled: true``, the WebUI workspace
    switcher will show 'workspace switching disabled'.
    """
    project_dir = _real_project_dir()
    app_config = AppConfig.from_yaml(project_dir / "config" / "bot_config.yml")
    assert app_config.workspace.enabled is True, (
        "bot_config.yml must set workspace.enabled: true; "
        "otherwise the WebUI workspace switcher shows 'workspace switching disabled'"
    )


@pytest.mark.asyncio
async def test_workspace_controller_allows_open_when_enabled() -> None:
    """Unit-level repro: WorkspaceController(enabled=True) allows /cd."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        target = Path(tmp) / "target_ws"
        target.mkdir()

        registry = WorkspaceRegistry(
            home=home,
            data_dir_name=".modex",
            factory=MagicMock(),  # type: ignore[arg-type]
            store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
        )
        controller = WorkspaceController(
            registry=registry,
            data_dir_name=".modex",
            enabled=True,
        )
        result = await controller.open_workspace(str(target))
        assert result.success is True
        assert result.current_path.resolve() == target.resolve()


@pytest.mark.asyncio
async def test_workspace_controller_rejects_open_when_disabled() -> None:
    """Unit-level repro: WorkspaceController(enabled=False) rejects /cd."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        target = Path(tmp) / "target_ws"
        target.mkdir()

        registry = WorkspaceRegistry(
            home=home,
            data_dir_name=".modex",
            factory=MagicMock(),  # type: ignore[arg-type]
            store=GlobalWorkspaceStore(home=home, data_dir_name=".modex"),
        )
        controller = WorkspaceController(
            registry=registry,
            data_dir_name=".modex",
            enabled=False,
        )
        result = await controller.open_workspace(str(target))
        assert result.success is False
        assert "workspace switching disabled" in result.notice
