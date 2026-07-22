"""Typed archive memory models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from modex_agent.runtime.models import JsonValue

ARCHIVE_SCHEMA = "archive"
CONTEXT_ARCHIVE_FILE_KEY = "context_archive"
CONTEXT_ARCHIVE_FILENAME = "context_archive.jsonl"
CORE_ARCHIVE_FILE_KEY = "core_archive"
CORE_ARCHIVE_FILENAME = "core_archive.jsonl"
DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS = 3


class ArchiveChannel(StrEnum):
    """Archive channel identifiers.

    ``CORE`` is the Core Memory channel (per ADR-0035; formerly ``KNOWLEDGE``).
    The string value ``"core"`` is serialized into ``*_archive.jsonl`` entries'
    ``channel`` field — it refers to the same concept as
    :attr:`modex_agent.core.scope.MemoryLayerName.CORE` (Core Memory); the
    short name ``"core"`` is kept for brevity in persisted JSON.
    """

    CONTEXT = "context"
    CORE = "core"


@dataclass(frozen=True)
class ArchiveWrite:
    channel: ArchiveChannel
    summary: str
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    raw_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        normalized_metadata: Mapping[str, JsonValue] = {
            **self.metadata,
            "schema": ARCHIVE_SCHEMA,
            "channel": self.channel.value,
        }
        object.__setattr__(self, "metadata", normalized_metadata)


@dataclass(frozen=True)
class ArchiveBundleResult:
    archive_id: int
    written_channels: tuple[ArchiveChannel, ...]


@dataclass(frozen=True)
class ArchiveState:
    next_archive_id: int = 1
    core_consumed_archive_id: int = 0


@dataclass(frozen=True)
class ArchiveInputStats:
    input_messages: int
    context_messages: int
    core_messages: int
    tool_chains: int
    dropped_messages: int


@dataclass(frozen=True)
class ArchiveGenerationInputs:
    context_transcript: str
    core_transcript: str
    stats: ArchiveInputStats


class ArchiveDocuments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    context: str
    core: str
    index: str

    @property
    def topic(self) -> str | None:
        return next((line.strip() for line in self.index.splitlines() if line.strip()), None)


class ArchiveGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    documents: ArchiveDocuments
    inputs: ArchiveGenerationInputs | None = None

    @property
    def writes(self) -> tuple[ArchiveWrite, ...]:
        return (
            ArchiveWrite(
                channel=ArchiveChannel.CONTEXT,
                summary=self.documents.context,
                metadata={"topic": self.documents.topic or ""},
            ),
            ArchiveWrite(
                channel=ArchiveChannel.CORE,
                summary=self.documents.core,
            ),
        )


__all__ = [
    "ARCHIVE_SCHEMA",
    "CONTEXT_ARCHIVE_FILE_KEY",
    "CONTEXT_ARCHIVE_FILENAME",
    "CORE_ARCHIVE_FILE_KEY",
    "CORE_ARCHIVE_FILENAME",
    "DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS",
    "ArchiveBundleResult",
    "ArchiveChannel",
    "ArchiveDocuments",
    "ArchiveGenerationInputs",
    "ArchiveGenerationResult",
    "ArchiveInputStats",
    "ArchiveState",
    "ArchiveWrite",
]
