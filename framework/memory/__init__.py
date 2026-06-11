"""Tiered memory system for agent context management.

Provides a three-layer architecture:
1. Short-term Memory — recent conversation history
2. History Archive — compressed summaries (medium-term)
3. Long-term Memory — user profile and knowledge

Key abstractions:
- MemorySystem: unified entry point
- MemoryContext: scope dimensions (session, user, tenant, etc.)
- MemoryScope: isolation strategy per layer
- MemoryInjectionPolicy: maps memory layers to LLM ContextState
"""

from framework.memory.cleanup import CleanupResult, cleanup_session
from framework.memory.context_governance import (
    CompositeGovernance,
    ContextGovernance,
    MicrocompactGovernance,
    TokenBudgetGovernance,
    ToolChainRepairGovernance,
    UserRetentionBufferInjectionGovernance,
)
from framework.memory.core.consolidation import (
    MemoryUpdate,
    MemoryUpdateMode,
)
from framework.memory.core.layers import (
    ArchiveMemoryManager,
    KnowledgeMemoryManager,
    MemoryLayerSet,
    SessionMemoryManager,
    UserRetentionBuffer,
)
from framework.memory.core.models import (
    ArchiveEntry,
    CompressionPlan,
    CompressionReason,
    CompressionTrigger,
    InjectionResult,
    KnowledgeBudget,
    LongTermMemory,
    MemoryBudget,
    StorageRevision,
)
from framework.memory.core.models import (
    CompressionResult as CompressionCommitResult,
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
from framework.memory.core.system import MemorySystem
from framework.memory.core.system import MemorySystem as MemorySystemABC
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.history_search import HistorySearchStrategy, KeywordHistorySearch
from framework.memory.injection import (
    FullInjectionPolicy,
    MemoryInjectionPolicy,
    RestrictedInjectionPolicy,
)
from framework.memory.layers import (
    ArchiveMemoryConfig,
    KnowledgeMemoryConfig,
    MemoryLayerConfigSet,
    MemoryLayerFactory,
    ScopedArchiveMemoryManager,
    ScopedKnowledgeMemoryManager,
    ScopedSessionMemoryManager,
    ScopedUserRetentionBuffer,
    SessionMemoryConfig,
    UserRetentionBufferConfig,
)
from framework.memory.pruned.manager import PrunedManager
from framework.memory.pruned.models import PrunedIndexEntry
from framework.memory.pruned.storage import FilePrunedStorage, PrunedStorage
from framework.memory.recorder import MemoryAppendRecorder, MemoryAppendSource
from framework.memory.registry import (
    DefaultMemoryStoreRegistry,
    InMemoryStoreRegistry,
    MemoryStoreRegistry,
)
from framework.memory.sanitizer import (
    DefaultSessionToolChainSanitizer,
    ToolChainSanitizationIssue,
    ToolChainSanitizationMode,
    ToolChainSanitizationReason,
    ToolChainSanitizationResult,
)
from framework.memory.system import (
    MemorySystemContextManager,
    create_memory_system,
)
from framework.memory.user_buffer import UserBufferEntry

__all__ = [
    # Entry points
    "MemorySystem",
    "MemorySystemABC",
    "DefaultMemorySystem",
    "create_memory_system",
    "MemorySystemContextManager",
    # Layer ownership
    "MemoryLayerSet",
    "SessionMemoryManager",
    "ArchiveMemoryManager",
    "KnowledgeMemoryManager",
    "UserRetentionBuffer",
    "UserRetentionBufferConfig",
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
    # Registry & storage
    "MemoryStoreRegistry",
    "DefaultMemoryStoreRegistry",
    "InMemoryStoreRegistry",
    "MemoryStorage",
    # Layer config & factory
    "MemoryLayerConfigSet",
    "MemoryLayerFactory",
    "SessionMemoryConfig",
    "ArchiveMemoryConfig",
    "KnowledgeMemoryConfig",
    "ScopedSessionMemoryManager",
    "ScopedArchiveMemoryManager",
    "ScopedKnowledgeMemoryManager",
    "ScopedUserRetentionBuffer",
    # User buffer
    "UserBufferEntry",
    # Shared models
    "ArchiveEntry",
    "CompressionPlan",
    "CompressionReason",
    "CompressionCommitResult",
    "CompressionTrigger",
    "InjectionResult",
    "KnowledgeBudget",
    "MemoryBudget",
    "StorageRevision",
    # Shared models
    "LongTermMemory",
    # Recorder
    "MemoryAppendRecorder",
    "MemoryAppendSource",
    # Tool-chain sanitizer
    "DefaultSessionToolChainSanitizer",
    "ToolChainSanitizationIssue",
    "ToolChainSanitizationMode",
    "ToolChainSanitizationReason",
    "ToolChainSanitizationResult",
    # Consolidation
    "MemoryUpdate",
    "MemoryUpdateMode",
    # Injection
    "MemoryInjectionPolicy",
    "FullInjectionPolicy",
    "RestrictedInjectionPolicy",
    # History search
    "HistorySearchStrategy",
    "KeywordHistorySearch",
    # Pruned catalog
    "FilePrunedStorage",
    "PrunedIndexEntry",
    "PrunedManager",
    "PrunedStorage",
    # Context governance
    "ContextGovernance",
    "CompositeGovernance",
    "ToolChainRepairGovernance",
    "UserRetentionBufferInjectionGovernance",
    "MicrocompactGovernance",
    "TokenBudgetGovernance",
    # Cleanup
    "cleanup_session",
    "CleanupResult",
]
