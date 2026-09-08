"""Memory-owned lifecycle hook contracts and dispatch."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.scope import MemoryContext

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
    CONTEXT_ASSEMBLED = "context_assembled"
    CORE_MEMORY_UPDATED = "core_memory_updated"
    CONSOLIDATION_FINISHED = "consolidation_finished"


class _FrozenMemoryPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LlmUsage(_FrozenMemoryPayload):
    """Model usage accumulated for one memory operation."""

    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class SectionProvenance(_FrozenMemoryPayload):
    """Token accounting for one retrieved context section."""

    source: str = Field(description="Stable name of the assembled memory section.")
    retrieved_tokens: int = Field(description="Tokens available before budget trimming.")
    injected_tokens: int = Field(description="Tokens retained in the assembled prompt.")
    pruned_tokens: int = Field(description="Retrieved tokens removed by budget trimming.")
    priority: int = Field(description="Section priority used by the trimming policy.")


class ContextAssembledPayload(_FrozenMemoryPayload):
    """Facts produced by one memory context assembly."""

    session_id: str
    agent: str
    duration_ms: float
    sections: list[SectionProvenance]


class MemoryUpdateRef(_FrozenMemoryPayload):
    """Content-safe reference to one core-memory update."""

    mode: str
    target: str
    content_digest: str = Field(
        description="Short content hash or truncated content reference, never full content."
    )


class CoreMemoryUpdatedPayload(_FrozenMemoryPayload):
    """Facts produced by one core-memory update."""

    session_id: str
    file: str
    update: MemoryUpdateRef
    idempotent: bool
    source_tag: str
    before_tokens: int
    after_tokens: int
    duration_ms: float


class ConsolidationFinishedPayload(_FrozenMemoryPayload):
    """Facts produced by one completed core or dream consolidation."""

    session_id: str
    trigger: Literal["core", "dream"]
    changed: bool
    consumed_count: int
    before_tokens: int
    after_tokens: int
    compression_ratio: float
    usage: LlmUsage | None
    duration_ms: float


class MemoryHookContext(BaseModel):
    """Immutable context shared with memory lifecycle hooks."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    session_manager: SessionMemoryManager | None = None
    memory_context: MemoryContext | None = None
    cleanup_result: CleanupResult | None = None
    compression_reason: CompressionReason | None = None
    archive_manager: ArchiveMemoryManager | None = None
    pruned_manager: PrunedManager | None = None
    context_assembled: ContextAssembledPayload | None = None
    core_memory_updated: CoreMemoryUpdatedPayload | None = None
    consolidation_finished: ConsolidationFinishedPayload | None = None


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


class ContextAssembledHook(MemoryHook):
    """Hook invoked after memory context assembly."""

    @abstractmethod
    async def on_context_assembled(self, ctx: MemoryHookContext) -> None: ...


class CoreMemoryUpdatedHook(MemoryHook):
    """Hook invoked after a core-memory update."""

    @abstractmethod
    async def on_core_memory_updated(self, ctx: MemoryHookContext) -> None: ...


class ConsolidationFinishedHook(MemoryHook):
    """Hook invoked after core or dream consolidation finishes."""

    @abstractmethod
    async def on_consolidation_finished(self, ctx: MemoryHookContext) -> None: ...


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


async def _call_context_assembled(
    hook: MemoryHook,
    ctx: MemoryHookContext,
) -> None:
    if isinstance(hook, ContextAssembledHook):
        await hook.on_context_assembled(ctx)


async def _call_core_memory_updated(
    hook: MemoryHook,
    ctx: MemoryHookContext,
) -> None:
    if isinstance(hook, CoreMemoryUpdatedHook):
        await hook.on_core_memory_updated(ctx)


async def _call_consolidation_finished(
    hook: MemoryHook,
    ctx: MemoryHookContext,
) -> None:
    if isinstance(hook, ConsolidationFinishedHook):
        await hook.on_consolidation_finished(ctx)


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
    MemoryHookPoint.CONTEXT_ASSEMBLED: (
        ContextAssembledHook,
        _call_context_assembled,
    ),
    MemoryHookPoint.CORE_MEMORY_UPDATED: (
        CoreMemoryUpdatedHook,
        _call_core_memory_updated,
    ),
    MemoryHookPoint.CONSOLIDATION_FINISHED: (
        ConsolidationFinishedHook,
        _call_consolidation_finished,
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
