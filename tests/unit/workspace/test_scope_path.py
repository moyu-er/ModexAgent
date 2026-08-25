"""Ticket 15 (SPEC §5.3) — ScopePath + the single scope-path resolver.

``ScopePath`` is the canonical addressing carrier into the scope tree;
``resolve_scope_path`` is the ONE function that resolves a pool path along
its parent chain: the workspace is the only materialization layer, so the
pool segment resolves inside the owning workspace's resource bundle
(``ws.pool_data.get(pool_name)``). The resolver never synthesizes a
process-CWD path — the ADR-0015 D5 escape property the legacy
``WorkspacePathResolver`` tests pinned (see
``tests/unit/multi_agent/test_workspace_path_escape.py``) lives on here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from modex_agent.pipeline.snapshot import PoolDataSnapshot
from modex_agent.workspace.scope_path import ScopePath, resolve_scope_path


@dataclass(frozen=True)
class _FakePoolData(PoolDataSnapshot):
    """Minimal concrete PoolDataSnapshot — a real frozen dataclass subclass
    (not a MagicMock) so field-name drift surfaces here (same pattern as the
    legacy workspace-paths tests)."""

    context_manager: Any
    turn_store: Any
    trace_store: Any | None = None
    memory_dir: Path | None = None
    runtime_dir: Path | None = None
    pruned_manager: Any | None = None
    experience_dir: Path | None = None


def _pool_data(runtime_dir: Path | None = None) -> _FakePoolData:
    return _FakePoolData(
        context_manager=MagicMock(),
        turn_store=MagicMock(),
        runtime_dir=runtime_dir,
    )


class TestScopePathValue:
    def test_frozen(self) -> None:
        path = ScopePath(workspace_root=Path("/ws"), pool_name="default")
        with pytest.raises(ValidationError):
            path.pool_name = "other"  # type: ignore[misc]

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScopePath(workspace_root=Path("/ws"), pool_name="default", bogus=1)

    def test_pool_name_defaults_to_none_workspace_level_address(self) -> None:
        path = ScopePath(workspace_root=Path("/ws"))
        assert path.pool_name is None

    def test_carries_both_segments(self) -> None:
        path = ScopePath(workspace_root=Path("/ws"), pool_name="coder")
        assert path.workspace_root == Path("/ws")
        assert path.pool_name == "coder"


class TestResolveScopePath:
    def test_pool_segment_resolves_within_workspace_bundle(self) -> None:
        data = _pool_data(runtime_dir=Path("/ws/.modex/runtime_state/coder"))
        manager = MagicMock()
        manager.resolve_workspace.return_value.pool_data.get.return_value = data

        resolved = resolve_scope_path(
            manager, ScopePath(workspace_root=Path("/ws"), pool_name="coder")
        )

        assert resolved is data
        manager.resolve_workspace.return_value.pool_data.get.assert_called_once_with("coder")

    def test_unknown_pool_returns_none_no_synthesis(self) -> None:
        manager = MagicMock()
        manager.resolve_workspace.return_value.pool_data.get.return_value = None

        assert (
            resolve_scope_path(
                manager, ScopePath(workspace_root=Path("/ws"), pool_name="ghost")
            )
            is None
        )

    def test_absent_manager_returns_none(self) -> None:
        assert (
            resolve_scope_path(
                None, ScopePath(workspace_root=Path("/ws"), pool_name="coder")
            )
            is None
        )

    def test_absent_path_returns_none(self) -> None:
        assert resolve_scope_path(MagicMock(), None) is None

    def test_workspace_level_address_has_no_pool_data(self) -> None:
        manager = MagicMock()
        assert (
            resolve_scope_path(manager, ScopePath(workspace_root=Path("/ws")))
            is None
        )
        manager.resolve_workspace.assert_not_called()

    def test_unmaterialized_workspace_returns_none(self) -> None:
        """resolve_workspace() raising RuntimeError (cell not yet filled) →
        None — the caller-visible half of the never-escape-to-CWD property."""
        manager = MagicMock()
        manager.resolve_workspace.side_effect = RuntimeError("not materialized")

        assert (
            resolve_scope_path(
                manager, ScopePath(workspace_root=Path("/ws"), pool_name="coder")
            )
            is None
        )
