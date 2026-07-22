"""Shared typed models for the tiered memory system."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from modex_agent.core.message import ChatMessage
from modex_agent.memory.archive_models import (
    ARCHIVE_SCHEMA,
    DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS,
    ArchiveBundleResult,
    ArchiveChannel,
    ArchiveGenerationInputs,
    ArchiveGenerationResult,
    ArchiveInputStats,
    ArchiveState,
    ArchiveWrite,
)
from modex_agent.memory.sanitizer import ToolChainSanitizationIssue


@dataclass(frozen=True)
class StorageRevision:
    """Revision metadata returned by scoped storage writes.

    ``updated_at`` is a Unix-epoch millisecond integer (ADR-0029 §6). Both
    file and SQLite backends pass ``now_ms()`` directly — no ``datetime``
    bridge at the adapter boundary.
    """

    message_count: int
    updated_at: int
    version: int = 0


class CompressionReason(StrEnum):
    TOKEN_PRESSURE = "token_pressure"
    MESSAGE_COUNT = "message_count"
    IDLE = "idle"
    MANUAL = "manual"
    SHUTDOWN = "shutdown"


class CompressionResultReason(StrEnum):
    """Reason codes for compression result outcomes."""

    NOT_NEEDED = "not_needed"
    EMPTY = "empty"
    NO_SAFE_BOUNDARY = "no_safe_boundary"
    REVISION_CHANGED = "revision_changed"
    ARCHIVE_FAILED = "archive_failed"
    USER_RETENTION_FAILED = "user_retention_failed"
    IDLE_EXPIRED = "idle_expired"
    NOTHING_TO_ARCHIVE = "nothing_to_archive"


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
    archive_generation_result: ArchiveGenerationResult | None = None
    user_retention_entries: list[Any] = field(default_factory=list)
    drop_without_archive_messages: list[dict[str, Any]] = field(default_factory=list)
    sanitization_issues: list[ToolChainSanitizationIssue] = field(default_factory=list)
    has_open_tail: bool = False
    idle_threshold_seconds: float | None = None


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
    reason: CompressionResultReason | None = None


@dataclass(frozen=True)
class MemoryBudget:
    max_system_prompt_tokens: int | None = None
    max_total_tokens: int | None = None


@dataclass(frozen=True)
class CoreMemoryBudget:
    """Capacity governance for the core memory (long-term) layer."""

    max_memory_chars: int = 8192
    max_custom_file_chars: int = 4096
    max_total_chars: int = 16384
    protected_files: tuple[str, ...] = ("SOUL.md", "USER.md")


@dataclass(frozen=True)
class InjectionResult:
    """Output of injection policy."""

    system_prompt: str
    messages: list[ChatMessage]


@dataclass
class CoreMemoryContents:
    """Long-term memory content container — SOUL.md, USER.md, MEMORY.md."""

    soul: str = ""
    user: str = ""
    memory: str = ""
    custom: dict[str, str] = field(default_factory=dict)
    _metadata: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def get_metadata(self, key: str) -> dict[str, Any]:
        return dict(self._metadata.get(key, {}))


from modex_agent.memory.core.consolidation import (  # noqa: E402
    MemoryUpdate,
    MemoryUpdateMode,
)

__all__ = [
    "ARCHIVE_SCHEMA",
    "DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS",
    "ArchiveBundleResult",
    "ArchiveChannel",
    "ArchiveEntry",
    "ArchiveGenerationInputs",
    "ArchiveGenerationResult",
    "ArchiveInputStats",
    "ArchiveState",
    "ArchiveWrite",
    "CompressionResult",
    "CompressionResultReason",
    "CompressionPlan",
    "CompressionReason",
    "CompressionTrigger",
    "CoreMemoryContents",
    "InjectionResult",
    "CoreMemoryBudget",
    "MemoryBudget",
    "MemoryUpdate",
    "MemoryUpdateMode",
    "StorageRevision",
    "UnprocessedResult",
]
