"""Tests for framework.workspace.routing.WorkspaceResolver (stub R + stub factory)."""

from __future__ import annotations

from pathlib import Path

from modex_agent.workspace.registry import ScopeRegistry
from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.workspace.store import GlobalWorkspaceStore

from ._stubs import StubFactory


async def test_resolve_home_returns_home_context_and_resources(tmp_path: Path) -> None:
    reg = ScopeRegistry(
        home=tmp_path, data_dir_name=".modex",
        factory=StubFactory(), store=GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex"),
    )
    resolver = WorkspaceResolver(registry=reg)
    ctx, r = await resolver.resolve(tmp_path)
    assert ctx.is_home is True
    assert r.target == ctx.target


async def test_resolve_workspace_targets_given_workspace(tmp_path: Path) -> None:
    target = tmp_path / "wsB"
    target.mkdir()
    reg = ScopeRegistry(
        home=tmp_path, data_dir_name=".modex",
        factory=StubFactory(), store=GlobalWorkspaceStore(home=tmp_path, data_dir_name=".modex"),
    )
    resolver = WorkspaceResolver(registry=reg)
    ctx, r = await resolver.resolve(target)
    assert ctx.target == target.resolve()
    assert r.target == target.resolve()
