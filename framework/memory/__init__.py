"""Tiered memory system for agent context management.

Provides a four-layer architecture:
1. Working Memory — current turn cache (in-memory)
2. Short-term Memory — recent conversation history
3. History Archive — compressed summaries (medium-term)
4. Long-term Memory — user profile and knowledge

Key abstractions:
- MemorySystem: unified entry point
- MemoryContext: scope dimensions (session, user, tenant, etc.)
- MemoryScope: isolation strategy per layer
- CompressionStrategy: short-term memory pruning
- ArchiveStrategy: how pruned messages are archived
- MemoryInjectionPolicy: maps memory layers to LLM ContextState
"""

from framework.memory.archive import (
    ArchiveStrategy,
    PreserveSummaryArchiveStrategy,
    RawDumpArchiveStrategy,
)
from framework.memory.core.compression import (
    CompressionContext,
    CompressionResult,
    CompressionStrategy,
    ImportanceScorer,
)
from framework.memory.core.scope import (
    AgentScope,
    ChannelScope,
    ChatScope,
    CompositeScope,
    GlobalScope,
    MemoryContext,
    MemoryScope,
    SessionScope,
    TenantScope,
    UserScope,
)
from framework.memory.core.storage import MemoryStorage
from framework.memory.injection import (
    DefaultMemoryInjectionPolicy,
    MemoryInjectionPolicy,
)
from framework.memory.managers.history import HistoryArchiveManager
from framework.memory.managers.long_term import (
    LongTermMemory,
    LongTermMemoryManager,
)
from framework.memory.managers.short_term import (
    ShortTermConfig,
    ShortTermMemoryManager,
)
from framework.memory.managers.working import WorkingMemoryManager
from framework.memory.stores.file import FileStorage
from framework.memory.stores.in_memory import InMemoryStorage
from framework.memory.system import (
    LayerConfig,
    MemorySystem,
    MemorySystemContextManager,
)

__all__ = [
    # Entry points
    "MemorySystem",
    "MemorySystemContextManager",
    "LayerConfig",
    # Context & scope
    "MemoryContext",
    "MemoryScope",
    "SessionScope",
    "UserScope",
    "TenantScope",
    "AgentScope",
    "ChannelScope",
    "ChatScope",
    "CompositeScope",
    "GlobalScope",
    # Storage backends
    "MemoryStorage",
    "InMemoryStorage",
    "FileStorage",
    # Managers
    "WorkingMemoryManager",
    "ShortTermConfig",
    "ShortTermMemoryManager",
    "HistoryArchiveManager",
    "LongTermMemory",
    "LongTermMemoryManager",
    # Compression
    "CompressionStrategy",
    "CompressionResult",
    "CompressionContext",
    "ImportanceScorer",
    # Archiving
    "ArchiveStrategy",
    "PreserveSummaryArchiveStrategy",
    "RawDumpArchiveStrategy",
    # Injection
    "MemoryInjectionPolicy",
    "DefaultMemoryInjectionPolicy",
]
