"""PoolResourceFactory — business ResourceFactory over PoolWorkspaceResources.

Thin orchestrator: the real per-workspace construction (workspace-level stores,
per-workspace broker/inbox/bus, per-pool data + pool instances, background tasks)
lives in the ``build_resources`` closure supplied by ``wiring.py``. This keeps the
factory unit-testable with fakes and lets the generic registry drive it with no
knowledge of how a workspace is built.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from bot.workspace.handle import PoolWorkspaceResources
from framework.workspace.context import WorkspaceContext
from framework.workspace.factory import ResourceFactory

BuildResources = Callable[[WorkspaceContext], Awaitable[PoolWorkspaceResources]]
StopResources = Callable[[PoolWorkspaceResources], Awaitable[None]]


class PoolResourceFactory(ResourceFactory[PoolWorkspaceResources]):
    """Build/evict a workspace's resources via injected closures."""

    def __init__(
        self,
        *,
        build_resources: BuildResources,
        stop_resources: StopResources,
    ) -> None:
        self._build_resources: BuildResources = build_resources
        self._stop_resources: StopResources = stop_resources

    async def materialize(self, ctx: WorkspaceContext) -> PoolWorkspaceResources:
        return await self._build_resources(ctx)

    async def evict(self, resources: PoolWorkspaceResources) -> None:
        await self._stop_resources(resources)
