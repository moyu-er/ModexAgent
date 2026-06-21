"""ResourceFactory — business extension point. The registry is generic over R."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

from framework.workspace.context import WorkspaceContext

R = TypeVar("R")


class ResourceFactory(ABC, Generic[R]):
    """Builds/tears down a workspace's heavy resources (business-supplied).

    Generic over the resource type ``R``. The generic package never names a
    concrete business resource type — business code subclasses this with its
    own ``R`` (see ``bot.workspace.bundle``).
    """

    @abstractmethod
    async def materialize(self, ctx: WorkspaceContext) -> R:
        """Build and return the per-workspace resource bundle for ``ctx``."""

    @abstractmethod
    async def evict(self, resources: R) -> None:
        """Release in-memory state held by ``resources`` (file state survives)."""
