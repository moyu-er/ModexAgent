"""Feedback loop for workspace switching disabled bug.

Reproduces the user-facing symptom: with the checked-in bot_config.yml,
workspace switching is disabled so POST /api/workspace/cd returns
``success: false`` with ``workspace switching disabled``.

Ticket 14: the ``workspace.enabled`` config flag is dead (N15). The stack
shape is selected by the scope declaration's form — a workspace-layer
declaration boots multi-live; its absence boots single-home.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bot.service.pool.declaration import workspace_layer_present

from modex_agent.persistence.config import PersistenceBackend
from modex_agent.scope.loader import load_scope_declaration
from modex_agent.scope.spec import ScopeKind
from modex_agent.workspace.control import WorkspaceController
from modex_agent.workspace.registry import ScopeRegistry
from modex_agent.workspace.store import GlobalWorkspaceStore


def _real_project_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


@pytest.mark.asyncio
async def test_checked_in_declaration_selects_multi_live_stack() -> None:
    """The checked-in scope declaration carries the workspace layer — the
    multi-live stack shape (the replacement for ``workspace.enabled: true``).

    Regression: with no workspace layer in bot.yml (and the config flag
    deleted), the WebUI workspace switcher would show 'workspace switching
    disabled'.
    """
    project_dir = _real_project_dir()
    spec = load_scope_declaration(project_dir / "config" / "scopes" / "bot.yml")
    assert spec.kind is ScopeKind.WORKSPACE
    assert spec.workspace is not None
    assert workspace_layer_present(spec) is True, (
        "bot.yml must carry the workspace layer; without it the WebUI "
        "workspace switcher shows 'workspace switching disabled'"
    )
    # Ticket 14: the shipped declaration carries the full resource-selection
    # face with values matching the service defaults (data landing
    # unchanged).
    assert spec.workspace.persistence is not None
    assert spec.workspace.persistence.backend is PersistenceBackend.SQLITE
    assert spec.workspace.paths is not None
    assert spec.workspace.paths.data_dir_name == ".modex"


@pytest.mark.asyncio
async def test_pool_as_root_and_absent_declarations_boot_single_home() -> None:
    """N15: declaration absence IS the single-workspace form — no config
    flag, no second mechanism."""
    assert workspace_layer_present(None) is False

    pool_root_spec = load_scope_declaration(
        Path(__file__).resolve().parents[4]
        / "tests"
        / "fixtures"
        / "scope"
        / "pool-as-root.yml"
    )
    assert pool_root_spec.kind is ScopeKind.POOL
    assert workspace_layer_present(pool_root_spec) is False


@pytest.mark.asyncio
async def test_workspace_controller_allows_open_when_enabled() -> None:
    """Unit-level repro: WorkspaceController(enabled=True) allows /cd."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        target = Path(tmp) / "target_ws"
        target.mkdir()

        registry = ScopeRegistry(
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

        registry = ScopeRegistry(
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
