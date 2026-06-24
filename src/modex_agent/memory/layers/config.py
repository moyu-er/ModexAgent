"""Configuration models for default memory layer construction.

Default session cleanup flow:

1. Session writes go through ``ScopedMessageHistory.append/extend``;
   both call ``cleanup_session()`` after the append.
2. ``cleanup_session()`` (in ``framework/memory/cleanup.py``) checks
   stored session messages against ``max_messages`` and ``max_tokens``.
3. When thresholds are exceeded, messages are pruned using the configured
   ``keep_ratio``. If an ``archive_strategy`` is provided, pruned messages
   are archived before removal.
4. ``UserBufferEntry`` records pruned unfinished
   ``user``/``agent`` inputs so ``UserRetentionBuffer`` can
   restore them into the next model-visible context until a plain assistant
   completion clears the user retention entries.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from modex_agent.memory.archive_models import DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS
from modex_agent.memory.core.scope import MemoryContext, MemoryScope, SessionScope, UserScope
from modex_agent.memory.core.storage import MemoryStorage

StorageFactory = Callable[[MemoryContext], Awaitable[MemoryStorage]]


@dataclass(frozen=True)
class SessionMemoryConfig:
    max_messages: int | None = 100
    scope: MemoryScope = field(default_factory=SessionScope)


@dataclass(frozen=True)
class ArchiveMemoryConfig:
    max_entries: int | None = 1000
    cursor_name: str = "default"
    scope: MemoryScope = field(default_factory=UserScope)
    retained_consumed_archive_pairs: int = DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS


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
    default_templates_dir: str | None = None


@dataclass(frozen=True)
class UserRetentionBufferConfig:
    enabled: bool = True
    max_entries: int = 3
    max_user_chars: int = 4000
    max_assistant_chars: int = 4000
    scope: MemoryScope = field(default_factory=SessionScope)


@dataclass(frozen=True)
class MemoryLayerConfigSet:
    session: SessionMemoryConfig = field(default_factory=SessionMemoryConfig)
    archive: ArchiveMemoryConfig | None = field(default_factory=ArchiveMemoryConfig)
    knowledge: KnowledgeMemoryConfig | None = field(default_factory=KnowledgeMemoryConfig)
    user_retention: UserRetentionBufferConfig | None = field(
        default_factory=UserRetentionBufferConfig
    )
