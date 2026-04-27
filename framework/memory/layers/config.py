"""Configuration models for default memory layer construction."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from framework.memory.core.scope import MemoryContext, MemoryScope, SessionScope, UserScope
from framework.memory.core.storage import MemoryStorage

StorageFactory = Callable[[MemoryContext], Awaitable[MemoryStorage]]


@dataclass(frozen=True)
class SessionMemoryConfig:
    max_messages: int | None = 100
    checkpoint_key: str = ".checkpoint"
    last_recovered_key: str = ".last_recovered_checkpoint"
    scope: MemoryScope = field(default_factory=SessionScope)


@dataclass(frozen=True)
class ArchiveMemoryConfig:
    max_entries: int | None = 1000
    cursor_name: str = "default"
    scope: MemoryScope = field(default_factory=UserScope)


@dataclass(frozen=True)
class KnowledgeMemoryConfig:
    scope: MemoryScope = field(default_factory=UserScope)
    default_files: dict[str, str] = field(
        default_factory=lambda: {
            "soul": "SOUL.md",
            "user": "USER.md",
            "memory": "MEMORY.md",
        }
    )
    max_changelog_entries: int | None = 1000


@dataclass(frozen=True)
class MemoryLayerConfigSet:
    session: SessionMemoryConfig = field(default_factory=SessionMemoryConfig)
    archive: ArchiveMemoryConfig | None = field(default_factory=ArchiveMemoryConfig)
    knowledge: KnowledgeMemoryConfig | None = field(default_factory=KnowledgeMemoryConfig)
