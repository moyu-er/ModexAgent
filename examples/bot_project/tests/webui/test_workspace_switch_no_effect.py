"""Regression: message routing follows the workspace carried on each message.

The resolver now routes by the workspace ``Path`` carried on the message
(filled by ResolveWorkspaceStage), not via a session-id-prefix -> workspace
map. These tests drive the resolver directly with a workspace path and assert
it lands on the target workspace (and home for the home path).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from framework.workspace.context import WorkspaceContext
from framework.workspace.registry import WorkspaceRegistry, RegistryStore
from framework.workspace.routing import WorkspaceResolver


class _FakeFactory:
    """ResourceFactory stub — materialize returns a trivial object keyed by
    target so the resolver path runs without building real pools/brokers."""

    async def materialize(self, ctx: WorkspaceContext) -> dict:
        return {"target": str(ctx.target)}

    async def evict(self, resources: dict) -> None:
        return None


def _build_resolver(home: Path, data_dir_name: str = ".modex"):
    store = _InMemoryRegistryStore()
    registry: WorkspaceRegistry = WorkspaceRegistry(
        home=home, data_dir_name=data_dir_name, factory=_FakeFactory(), store=store
    )
    return registry, WorkspaceResolver(registry=registry)


class _InMemoryRegistryStore(RegistryStore):
    def __init__(self) -> None:
        self._targets: list[Path] = []

    def load_known_targets(self) -> list[Path]:
        return list(self._targets)

    def save_known_targets(self, targets: list[Path]) -> None:
        self._targets = list(targets)


@pytest.mark.asyncio
async def test_resolve_target_workspace_routes_to_target() -> None:
    """Resolving a target workspace path lands on that workspace (not home)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        target = Path(tmp) / "target_ws"
        target.mkdir()

        _registry, resolver = _build_resolver(home)

        # Resolve the workspace path carried on the message.
        ctx, _resources = await resolver.resolve(target)
        assert Path(ctx.target).resolve() == target.resolve(), (
            f"Expected {target}, got {ctx.target}"
        )


@pytest.mark.asyncio
async def test_resolve_target_workspace_then_home_routes_distinctly() -> None:
    """Resolving target then home yields distinct workspace contexts."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        target = Path(tmp) / "target_ws"
        target.mkdir()

        _registry, resolver = _build_resolver(home)

        ctx_target, _ = await resolver.resolve(target)
        ctx_home, _ = await resolver.resolve(home)
        assert Path(ctx_target.target).resolve() == target.resolve()
        assert Path(ctx_home.target).resolve() == home.resolve()
        assert ctx_target.target != ctx_home.target


@pytest.mark.asyncio
async def test_resolve_home_routes_to_home() -> None:
    """Resolving the home workspace path lands on home."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()

        _registry, resolver = _build_resolver(home)

        ctx, _resources = await resolver.resolve(home)
        assert Path(ctx.target).resolve() == home.resolve()
