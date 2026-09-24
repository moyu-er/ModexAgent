"""Shared fixtures for memory tests."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from modex_agent.core import MessageHistory
from modex_agent.core.message import ChatMessage
from modex_agent.memory.core.system import ContextManagedMemorySystem
from modex_agent.memory.scope import MemoryContext
from modex_agent.memory.token_estimator import TokenEstimator


class FixedTokenEstimator(TokenEstimator):
    """Deterministic estimator: estimate_text returns ``per_message`` (default 10).

    estimate_message therefore returns per_message + MESSAGE_OVERHEAD (4) per message.
    """

    def __init__(self, per_message: int = 10) -> None:
        self.per_message = per_message

    def estimate_text(self, text: str) -> int:
        return self.per_message


class NoCompactionMemorySystem(ContextManagedMemorySystem):
    """Compaction-capability-free deployment: inherits the ABC-default
    ``compact_session`` no-op (always returns ``triggered=False``).

    Doubles as the SUT for the ABC-default no-op test and as the
    memory-system reference for ``ScopedMessageHistory`` constructions that
    do not exercise the cleanup pipeline.
    """

    async def ensure_within_budget(self, context: MemoryContext) -> None:
        _ = context

    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> MessageHistory:
        raise NotImplementedError

    async def get_history(self, context: MemoryContext) -> list[ChatMessage]:
        return []

    async def clear(self, context: MemoryContext) -> None:
        _ = context


@pytest.fixture
def noop_memory_system() -> NoCompactionMemorySystem:
    return NoCompactionMemorySystem()
