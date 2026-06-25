"""Shared stub resource + factory for generic-package unit tests (no business deps)."""

from __future__ import annotations

from pathlib import Path

from modex_agent.workspace.context import WorkspaceContext
from modex_agent.workspace.factory import ResourceFactory


class StubResources:
    """Minimal stand-in for a business resource bundle R."""

    def __init__(self, target: Path) -> None:
        self.target = target
        self.evicted: bool = False


class StubFactory(ResourceFactory[StubResources]):
    def __init__(self) -> None:
        self.calls: list[Path] = []

    async def materialize(self, ctx: WorkspaceContext) -> StubResources:
        self.calls.append(ctx.target)
        return StubResources(ctx.target)

    async def evict(self, resources: StubResources) -> None:
        resources.evicted = True
