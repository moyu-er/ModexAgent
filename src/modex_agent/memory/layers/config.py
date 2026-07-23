"""Configuration models for default memory layer construction.

Default session cleanup flow:

1. Session writes go through ``ScopedMessageHistory.append/extend``;
   both call ``cleanup_session()`` after the append.
2. ``cleanup_session()`` (in ``framework/memory/cleanup.py``) is
   token-based: it compresses when non-system session tokens exceed
   ``max_context_tokens * max_token_ratio``, keeping a tail within
   ``max_context_tokens * keep_ratio``.
3. When the threshold is exceeded, messages are pruned using the configured
   ``keep_ratio``. If an ``archive_strategy`` is provided, pruned messages
   are archived before removal.
4. ``UserBufferEntry`` records pruned unfinished
   ``user``/``agent`` inputs so ``UserRetentionBuffer`` can
   restore them into the next model-visible context until a plain assistant
   completion clears the user retention entries.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.core.scope import MemoryContext, Scope, SessionScope, UserScope
from modex_agent.memory.archive_models import DEFAULT_RETAINED_CONSUMED_ARCHIVE_PAIRS
from modex_agent.memory.core.split_stores import MemoryStoreBundle

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


class UserRetentionBufferConfig(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    enabled: bool = True
    max_entries: int = 3
    max_user_chars: int = 4000
    max_assistant_chars: int = 4000
    scope: Scope = Field(default_factory=SessionScope)


class MemoryLayerConfigSet(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    session: SessionMemoryConfig = Field(default_factory=SessionMemoryConfig)
    archive: ArchiveMemoryConfig | None = Field(default_factory=ArchiveMemoryConfig)
    core: CoreMemoryConfig | None = Field(default_factory=CoreMemoryConfig)
    user_retention: UserRetentionBufferConfig | None = Field(
        default_factory=UserRetentionBufferConfig
    )
