"""Workspace resolution (registry lookup + materialization).

The :class:`WorkspaceResolver` resolves a workspace path to its
:class:`WorkspaceContext` and materialized resources ``R``.
"""
from __future__ import annotations

from pathlib import Path

from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.registry import ScopeRegistry


class WorkspaceResolver[R]:
    """Resolve a workspace path to its (WorkspaceContext, materialized R).

    Opens/registers the workspace context in the registry for the given
    workspace target and lazily materializes its resources. Generic over
    ``R`` so the package stays business-agnostic.
    """

    def __init__(
        self,
        *,
        registry: ScopeRegistry[R],
    ) -> None:
        self._registry: ScopeRegistry[R] = registry

    async def resolve(self, ws: Path) -> tuple[WorkspaceContext, R]:
        ctx = await self._registry.get_or_open(ws)
        resources = await self._registry.materialize(ctx)
        return ctx, resources

    def begin_turn(self, target: Path) -> None:
        """Mark a turn as in-flight on ``target``'s workspace (eviction-protected)."""
        self._registry.begin_turn(target)

    def end_turn(self, target: Path) -> None:
        """Mark a turn on ``target``'s workspace as complete."""
        self._registry.end_turn(target)
