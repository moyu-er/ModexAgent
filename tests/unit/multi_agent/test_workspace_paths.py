"""Tests for WorkspacePathResolver — workspace-aware path resolution."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
from modex_agent.pipeline.snapshot import PoolDataSnapshot


@dataclass(frozen=True)
class _FakePoolData(PoolDataSnapshot):
    """Minimal concrete PoolDataSnapshot for path-resolution tests.

    A real (frozen dataclass) subclass — not a MagicMock — so attribute
    access is checked against PoolDataSnapshot's declared fields and
    field-name drift surfaces here rather than being silently swallowed.
    PoolDataSnapshot is an ABC whose required fields (context_manager,
    turn_store) are heavy to construct for a path-resolution unit test,
    so they're typed Any and given MagicMock sentinels.
    """

    context_manager: Any
    turn_store: Any
    trace_store: Any | None = None
    memory_dir: Path | None = None
    runtime_dir: Path | None = None
    pruned_manager: Any | None = None
    experience_dir: Path | None = None


def test_runtime_dir_prefers_workspace_pool_data() -> None:
    pool_data = _FakePoolData(
        context_manager=MagicMock(), turn_store=MagicMock(), runtime_dir=Path("/ws/runtime"),
    )
    ws_mgr = MagicMock()
    ws_mgr.resolve_workspace.return_value.pool_data.get.return_value = pool_data
    resolver = WorkspacePathResolver(
        workspace_manager=ws_mgr, pool_name="main", fallback_runtime_dir=Path("/fb"),
    )
    assert resolver.runtime_dir() == Path("/ws/runtime")


def test_runtime_dir_falls_back_to_ctor_arg() -> None:
    ws_mgr = MagicMock()
    ws_mgr.resolve_workspace.return_value.pool_data.get.return_value = None
    resolver = WorkspacePathResolver(
        workspace_manager=ws_mgr, pool_name="main", fallback_runtime_dir=Path("/fb"),
    )
    assert resolver.runtime_dir() == Path("/fb")


def test_runtime_dir_returns_none_when_no_workspace() -> None:
    resolver = WorkspacePathResolver(
        workspace_manager=None, pool_name="main", fallback_runtime_dir=None,
    )
    assert resolver.runtime_dir() is None


def test_runtime_dir_returns_none_when_workspace_unmaterialized() -> None:
    """resolve_workspace() raising RuntimeError (no active workspace) -> None.

    Exercises the try/except catch that relocation fidelity depends on.
    """
    ws_mgr = MagicMock()
    ws_mgr.resolve_workspace.side_effect = RuntimeError
    resolver = WorkspacePathResolver(
        workspace_manager=ws_mgr, pool_name="main", fallback_runtime_dir=None,
    )
    assert resolver.runtime_dir() is None


def test_output_path_assembles_under_runtime_dir() -> None:
    pool_data = _FakePoolData(
        context_manager=MagicMock(), turn_store=MagicMock(), runtime_dir=Path("/ws/runtime"),
    )
    ws_mgr = MagicMock()
    ws_mgr.resolve_workspace.return_value.pool_data.get.return_value = pool_data
    resolver = WorkspacePathResolver(
        workspace_manager=ws_mgr, pool_name="main", fallback_runtime_dir=Path("/fb"),
    )
    p = resolver.output_path("pfx.main")
    assert p == Path("/ws/runtime/output/pfx.main/OUTPUT.md")


def test_output_path_returns_none_when_runtime_unresolved() -> None:
    resolver = WorkspacePathResolver(
        workspace_manager=None, pool_name="main", fallback_runtime_dir=None,
    )
    assert resolver.output_path("x.main") is None


def test_trace_dir_assembles_under_runtime_dir() -> None:
    pool_data = _FakePoolData(
        context_manager=MagicMock(), turn_store=MagicMock(), runtime_dir=Path("/ws/runtime"),
    )
    ws_mgr = MagicMock()
    ws_mgr.resolve_workspace.return_value.pool_data.get.return_value = pool_data
    resolver = WorkspacePathResolver(
        workspace_manager=ws_mgr, pool_name="main", fallback_runtime_dir=Path("/fb"),
    )
    assert resolver.trace_dir("pfx.main") == Path("/ws/runtime/trace/pfx.main")


def test_trace_dir_returns_none_when_runtime_unresolved() -> None:
    resolver = WorkspacePathResolver(
        workspace_manager=None, pool_name="main", fallback_runtime_dir=None,
    )
    assert resolver.trace_dir("x.main") is None


def test_memory_dir_prefers_workspace() -> None:
    pool_data = _FakePoolData(
        context_manager=MagicMock(), turn_store=MagicMock(), memory_dir=Path("/ws/memory"),
    )
    ws_mgr = MagicMock()
    ws_mgr.resolve_workspace.return_value.pool_data.get.return_value = pool_data
    resolver = WorkspacePathResolver(
        workspace_manager=ws_mgr, pool_name="main", fallback_memory_dir=Path("/fb"),
    )
    assert resolver.memory_dir() == Path("/ws/memory")


def test_pruned_manager_prefers_workspace() -> None:
    pruned = MagicMock(name="ws_pruned")
    pool_data = _FakePoolData(
        context_manager=MagicMock(), turn_store=MagicMock(), pruned_manager=pruned,
    )
    ws_mgr = MagicMock()
    ws_mgr.resolve_workspace.return_value.pool_data.get.return_value = pool_data
    fallback = MagicMock(name="fb_pruned")
    resolver = WorkspacePathResolver(
        workspace_manager=ws_mgr, pool_name="main", fallback_pruned_manager=fallback,
    )
    assert resolver.pruned_manager() is pruned


def test_pruned_manager_falls_back_to_ctor() -> None:
    ws_mgr = MagicMock()
    ws_mgr.resolve_workspace.return_value.pool_data.get.return_value = None
    fallback = MagicMock(name="fb_pruned")
    resolver = WorkspacePathResolver(
        workspace_manager=ws_mgr, pool_name="main", fallback_pruned_manager=fallback,
    )
    assert resolver.pruned_manager() is fallback
