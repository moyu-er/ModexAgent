"""Memory cleanup event listener protocol.

Allows bot/business layers to observe session cleanup (compaction) events —
e.g. to notify the user that memory is being consolidated — without the memory
layer taking a dependency on output adapters or agent runtime.

Defined as an ABC (per architecture rules) with TYPE_CHECKING-only imports so it
stays a pure protocol with no runtime coupling to heavy memory modules.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modex_agent.core.scope import MemoryContext
    from modex_agent.memory.cleanup import CleanupResult
    from modex_agent.memory.core.models import CompressionReason


class MemoryCleanupListener(ABC):
    """Observer for session cleanup (compaction) lifecycle events.

    ``on_cleanup_triggered`` fires once a cleanup trigger is confirmed and
    *before* the (potentially slow) archive-generation LLM call — the right
    moment to tell a user "consolidating memory, please wait".

    ``on_cleanup_finished`` fires after the full cleanup orchestrator returns,
    carrying the :class:`CleanupResult` (only called when ``result.triggered``).
    """

    @abstractmethod
    async def on_cleanup_triggered(
        self, context: MemoryContext, reason: CompressionReason
    ) -> None:
        """A cleanup was triggered and is about to run (pre-archive)."""
        ...

    @abstractmethod
    async def on_cleanup_finished(
        self, context: MemoryContext, result: CleanupResult
    ) -> None:
        """A triggered cleanup has completed."""
        ...
