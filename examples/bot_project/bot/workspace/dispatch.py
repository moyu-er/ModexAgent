"""Top-level inbound router (business).

Reads the shared InputAdapter; for each inbound message resolves the
conversation's workspace, binds ``current_workspace_root`` for the turn, then
routes into that workspace's pool router. Replaces the single global
``PoolRouter.run()``: routing is now per-message into the conversation's
workspace (multi-live).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Generic, TypeVar

from modex_agent.workspace.routing import WorkspaceResolver
from modex_agent.workspace.runtime import bind_workspace_root

logger = logging.getLogger(__name__)

M = TypeVar("M")
R = TypeVar("R")


class WorkspaceMessageDispatcher(Generic[M, R]):
    """Per-message workspace router. Generic over message type M and resource type R."""

    def __init__(
        self,
        *,
        receive: Callable[[], AsyncIterator[M]],
        resolver: WorkspaceResolver[R],
        workspace_of: Callable[[M], Path],
        route_one: Callable[[R, M], Awaitable[None]],
    ) -> None:
        self._receive: Callable[[], AsyncIterator[M]] = receive
        # InputAdapter.receive() is an async generator; hold the iterator so
        # each dispatch_once() advances the same stream instead of restarting it.
        self._receive_iter: AsyncIterator[M] = receive()
        self._resolver: WorkspaceResolver[R] = resolver
        self._workspace_of: Callable[[M], Path] = workspace_of
        self._route_one: Callable[[R, M], Awaitable[None]] = route_one

    async def dispatch_once(self) -> None:
        """Resolve one message's workspace, bind its root for the turn, route it."""
        message = await self._receive_iter.__anext__()
        ws = self._workspace_of(message)
        ctx, resources = await self._resolver.resolve(ws)
        # Mark the turn in-flight so LRU eviction cannot stop this workspace's
        # broker/cancel its background tasks while the turn is still running.
        self._resolver.begin_turn(ctx.target)
        try:
            with bind_workspace_root(ctx.target):
                await self._route_one(resources, message)
        finally:
            self._resolver.end_turn(ctx.target)

    async def run(self) -> None:
        """Dispatch loop: ``StopAsyncIteration`` from receive ends the loop cleanly."""
        while True:
            try:
                await self.dispatch_once()
            except StopAsyncIteration:
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("workspace dispatcher error; continuing")
