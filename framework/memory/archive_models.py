"""Typed archive memory models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from framework.runtime.models import JsonValue

ARCHIVE_SCHEMA = "archive"
CONTEXT_ARCHIVE_FILE_KEY = "context_archive"
CONTEXT_ARCHIVE_FILENAME = "context_archive.jsonl"
KNOWLEDGE_ARCHIVE_FILE_KEY = "knowledge_archive"
KNOWLEDGE_ARCHIVE_FILENAME = "knowledge_archive.jsonl"
DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS = 3


class ArchiveChannel(StrEnum):
    CONTEXT = "context"
    KNOWLEDGE = "knowledge"


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
    knowledge_consumed_archive_id: int = 0


@dataclass(frozen=True)
class ArchiveInputStats:
    input_messages: int
    context_messages: int
    knowledge_messages: int
    tool_chains: int
    dropped_messages: int


@dataclass(frozen=True)
class ArchiveGenerationInputs:
    context_transcript: str
    knowledge_transcript: str
    stats: ArchiveInputStats


@dataclass(frozen=True)
class ArchiveGenerationResult:
    writes: tuple[ArchiveWrite, ...]
    inputs: ArchiveGenerationInputs


__all__ = [
    "ARCHIVE_SCHEMA",
    "CONTEXT_ARCHIVE_FILE_KEY",
    "CONTEXT_ARCHIVE_FILENAME",
    "DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS",
    "KNOWLEDGE_ARCHIVE_FILE_KEY",
    "KNOWLEDGE_ARCHIVE_FILENAME",
    "ArchiveBundleResult",
    "ArchiveChannel",
    "ArchiveGenerationInputs",
    "ArchiveGenerationResult",
    "ArchiveInputStats",
    "ArchiveState",
    "ArchiveWrite",
]
