"""Memory-owned lifecycle hook contracts and dispatch."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from modex_agent.core.scope import MemoryContext
from modex_agent.memory.pruned.manager import PrunedManager

if TYPE_CHECKING:
    from modex_agent.memory.cleanup import CleanupResult
    from modex_agent.memory.core.layers import ArchiveMemoryManager, SessionMemoryManager
    from modex_agent.memory.core.models import CompressionReason
else:
    CleanupResult = object
    ArchiveMemoryManager = object
    SessionMemoryManager = object
    CompressionReason = object

logger = logging.getLogger(__name__)

_DEFAULT_MEMORY_HOOK_TIMEOUT = 10.0


class MemoryHookPoint(StrEnum):
    """Memory lifecycle hook dispatch points."""

    CLEANUP_TRIGGERED = "cleanup_triggered"
    CLEANUP_FINISHED = "cleanup_finished"


class MemoryHookContext(BaseModel):
    """Immutable context shared with memory lifecycle hooks."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    session_manager: SessionMemoryManager | None = None
    memory_context: MemoryContext | None = None
    cleanup_result: CleanupResult | None = None
    compression_reason: CompressionReason | None = None
    archive_manager: ArchiveMemoryManager | None = None
    pruned_manager: PrunedManager | None = None


class MemoryHook(ABC):  # noqa: B024
    """Base class for memory-owned lifecycle hooks."""


class CleanupTriggeredHook(MemoryHook):
    """Hook invoked when cleanup is triggered."""

    @abstractmethod
    async def on_cleanup_triggered(self, ctx: MemoryHookContext) -> None: ...


class CleanupFinishedHook(MemoryHook):
    """Hook invoked after cleanup finishes."""

    @abstractmethod
    async def on_cleanup_finished(self, ctx: MemoryHookContext) -> None: ...


async def _call_cleanup_triggered(
    hook: MemoryHook,
    ctx: MemoryHookContext,
) -> None:
    if isinstance(hook, CleanupTriggeredHook):
        await hook.on_cleanup_triggered(ctx)


async def _call_cleanup_finished(
    hook: MemoryHook,
    ctx: MemoryHookContext,
) -> None:
    if isinstance(hook, CleanupFinishedHook):
        await hook.on_cleanup_finished(ctx)


MemoryHookCaller = Callable[[MemoryHook, MemoryHookContext], Awaitable[None]]

_MEMORY_HOOK_DISPATCH: dict[
    MemoryHookPoint,
    tuple[type[MemoryHook], MemoryHookCaller],
] = {
    MemoryHookPoint.CLEANUP_TRIGGERED: (
        CleanupTriggeredHook,
        _call_cleanup_triggered,
    ),
    MemoryHookPoint.CLEANUP_FINISHED: (
        CleanupFinishedHook,
        _call_cleanup_finished,
    ),
}


class MemoryHookRunner:
    """Dispatch memory lifecycle hooks with per-hook failure isolation."""

    def __init__(self) -> None:
        self._hooks: list[MemoryHook] = []

    def add(self, hook: MemoryHook) -> None:
        """Register a memory hook for subsequent dispatches."""
        self._hooks.append(hook)

    async def dispatch(
        self,
        point: MemoryHookPoint,
        ctx: MemoryHookContext,
        *,
        timeout: float | None = None,
    ) -> None:
        """Dispatch one lifecycle point to matching hooks in registration order."""
        effective_timeout = (
            timeout if timeout is not None else _DEFAULT_MEMORY_HOOK_TIMEOUT
        )
        abc_type, caller = _MEMORY_HOOK_DISPATCH[point]
        snapshot = tuple(self._hooks)

        for hook in snapshot:
            if not isinstance(hook, abc_type):
                continue
            try:
                await asyncio.wait_for(caller(hook, ctx), timeout=effective_timeout)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                logger.warning(
                    "Memory hook %s timed out at %s after %.1fs",
                    type(hook).__name__,
                    point.value,
                    effective_timeout,
                )
            except Exception:
                logger.warning(
                    "Memory hook %s failed at %s",
                    type(hook).__name__,
                    point.value,
                    exc_info=True,
                )
