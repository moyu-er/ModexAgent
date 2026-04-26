"""Shared typed models for the tiered memory system."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from framework.memory.core.message import ChatMessage


@dataclass(frozen=True)
class StorageRevision:
    """Revision metadata returned by scoped storage writes."""

    message_count: int
    updated_at: datetime
    version: int = 0


class CompressionReason(StrEnum):
    TOKEN_PRESSURE = "token_pressure"
    MESSAGE_COUNT = "message_count"
    IDLE = "idle"
    MANUAL = "manual"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True)
class CompressionTrigger:
    reason: CompressionReason
    score: float = 1.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompressionPlan:
    trigger: CompressionTrigger
    expected_revision: StorageRevision
    expected_cursor: int | None
    keep_messages: list[dict[str, Any]]
    summarize_messages: list[dict[str, Any]]
    archive_raw_messages: list[dict[str, Any]]
    drop_messages: list[dict[str, Any]]
    summary: str | None = None


@dataclass(frozen=True)
class ArchiveEntry:
    summary: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    entry_id: int | None = None
    created_at: datetime | None = None
    raw_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class UnprocessedResult:
    cursor: int
    entries: list[ArchiveEntry]


@dataclass(frozen=True)
class CompressionResult:
    committed: bool
    retryable: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class MemoryBudget:
    max_system_prompt_tokens: int | None = None
    max_total_tokens: int | None = None
    max_history_messages: int = 50


@dataclass(frozen=True)
class KnowledgeBudget:
    """Capacity governance for the knowledge (long-term) memory layer."""

    max_memory_chars: int = 8192
    max_custom_file_chars: int = 4096
    max_total_chars: int = 16384
    protected_files: tuple[str, ...] = ("SOUL.md", "USER.md")


@dataclass(frozen=True)
class PromptSection:
    key: str
    content: str
    priority: int = 0
    source: str = "system"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryContextBundle:
    system_sections: list[PromptSection]
    messages: list[ChatMessage]
    compression_summary: str | None = None
    auto_compact_summary: str | None = None
    dropped_sections: list[dict[str, Any]] = field(default_factory=list)
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class LongTermMemory:
    """Long-term memory content container — SOUL.md, USER.md, MEMORY.md."""

    soul: str = ""
    user: str = ""
    memory: str = ""
    custom: dict[str, str] = field(default_factory=dict)
    _metadata: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def get_metadata(self, key: str) -> dict[str, Any]:
        return dict(self._metadata.get(key, {}))


from framework.memory.core.consolidation import (  # noqa: E402
    ConsolidationResult,
    MemoryUpdate,
    MemoryUpdateMode,
)

__all__ = [
    "ArchiveEntry",
    "CompressionResult",
    "CompressionPlan",
    "CompressionReason",
    "CompressionTrigger",
    "ConsolidationResult",
    "LongTermMemory",
    "KnowledgeBudget",
    "MemoryBudget",
    "MemoryContextBundle",
    "MemoryUpdate",
    "MemoryUpdateMode",
    "PromptSection",
    "StorageRevision",
    "UnprocessedResult",
]
