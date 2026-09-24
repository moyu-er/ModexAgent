"""Configuration models for default memory layer construction.

Default session cleanup flow:

1. Session writes go through ``ScopedMessageHistory.append/extend``;
   both trigger the memory system's ``compact_session()`` (which runs the
   ``cleanup_session()`` engine) after the append.
2. ``cleanup_session()`` (in ``framework/memory/cleanup.py``) is
   token-based: it compresses when non-system session tokens exceed the
   dual-condition trigger, keeping a tail within the absolute token budget
   ``clamp(usable × 0.25, 2000, 15000)`` (PRD §4.4.7).
3. If an ``archive_strategy`` is provided, pruned messages are archived
   before removal.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.memory.archive_models import DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS
from modex_agent.memory.core.split_stores import MemoryStoreBundle
from modex_agent.memory.scope import MemoryContext, Scope, SessionScope, UserScope

StorageFactory = Callable[[MemoryContext], Awaitable[MemoryStoreBundle]]


class SessionMemoryConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    scope: Scope = Field(default_factory=SessionScope)


class ArchiveMemoryConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    max_entries: int | None = 1000
    cursor_name: str = "default"
    scope: Scope = Field(default_factory=UserScope)
    retained_consumed_archive_pairs: int = DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS
    max_archive_total: int | None = Field(
        default=None,
        description=(
            "Maximum number of archive directories on disk. When set, "
            "append_bundle FIFO-evicts oldest consumed archive entries "
            "(archive_id <= core_consumed_archive_id) once the count "
            "exceeds this cap. Unconsumed archives are never deleted. "
            "Mirrors DefaultMemoryMaintenancePolicy.scan_once semantics."
        ),
    )


class CoreMemoryConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    scope: Scope = Field(default_factory=UserScope)
    default_files: dict[str, str] = Field(
        default_factory=lambda: {
            "soul": "SOUL.md",
            "user": "USER.md",
            "memory": "MEMORY.md",
        }
    )
    max_changelog_entries: int | None = 1000
    default_templates_dir: str | None = None


class MemoryLayerConfigSet(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    session: SessionMemoryConfig = Field(default_factory=SessionMemoryConfig)
    archive: ArchiveMemoryConfig | None = Field(default_factory=ArchiveMemoryConfig)
    core: CoreMemoryConfig | None = Field(default_factory=CoreMemoryConfig)
