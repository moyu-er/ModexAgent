"""Configuration models for default memory layer construction.

Default persistent session compression flow:

1. Session writes go through ``DefaultMemorySystem.add_messages`` or
   ``ScopedMessageHistory.append/extend``; both call
   ``DefaultMemoryLifecyclePolicy.on_messages_added`` after the append.
2. The lifecycle policy calls
   ``DefaultMemoryCompressionCoordinator.maybe_compress``. It does not
   inspect ReAct tool-call state itself.
3. ``DefaultCompressionTriggerPolicy`` checks all stored session messages
   against ``max_messages`` and ``max_tokens``. The trigger is strict:
   compression starts when the stored count/token estimate is greater than
   the configured limit.
4. ``DefaultSessionToolChainSanitizer`` removes invalid stored tool-chain
   records before planning: orphan ``tool`` messages, stale incomplete
   ``assistant(tool_calls)`` groups, duplicate tool results, and partial
   tools attached to stale groups. The final active incomplete
   ``assistant(tool_calls)`` tail is preserved.
5. ``PriorityCompressionKeepPlanner`` applies keep ratios and retention
   priorities. It may prune/archive older complete ReAct chains while
   protecting only the active open tail as an indivisible suffix.
6. ``DefaultPendingPrunedInputExtractor`` records pruned unfinished
   ``user``/``agent`` inputs so ``DefaultPendingPrunedInputInjector`` can
   restore them into the next model-visible context until a plain assistant
   completion clears the pending entries.
7. ``DefaultCommitPolicy`` writes archive summaries first when an archive
   layer exists, then replaces session messages with the keep set. When
   ``archive=None`` the same planning path still trims session memory, but
   no archive entry is written.
"""

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
class PendingPrunedInputMemoryConfig:
    enabled: bool = True
    max_entries: int = 8
    max_chars: int = 12000
    scope: MemoryScope = field(default_factory=SessionScope)


@dataclass(frozen=True)
class MemoryLayerConfigSet:
    session: SessionMemoryConfig = field(default_factory=SessionMemoryConfig)
    archive: ArchiveMemoryConfig | None = field(default_factory=ArchiveMemoryConfig)
    knowledge: KnowledgeMemoryConfig | None = field(default_factory=KnowledgeMemoryConfig)
    pending: PendingPrunedInputMemoryConfig | None = field(
        default_factory=PendingPrunedInputMemoryConfig
    )
