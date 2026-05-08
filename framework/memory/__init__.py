"""Tiered memory system for agent context management.

Provides a three-layer architecture:
1. Short-term Memory — recent conversation history
2. History Archive — compressed summaries (medium-term)
3. Long-term Memory — user profile and knowledge

Key abstractions:
- MemorySystem: unified entry point
- MemoryContext: scope dimensions (session, user, tenant, etc.)
- MemoryScope: isolation strategy per layer
- MemoryCompressionCoordinator: unified session-to-archive compaction
- ArchiveStrategy: how pruned messages are archived
- MemoryInjectionPolicy: maps memory layers to LLM ContextState
"""

from framework.memory.archive import (
    ArchiveStrategy,
    PreserveSummaryArchiveStrategy,
    RawDumpArchiveStrategy,
)
from framework.memory.compaction.boundary import (
    BoundaryPolicy,
    ToolChainBoundaryPolicy,
    UserTurnToolChainBoundaryPolicy,
)
from framework.memory.compaction.policy import (
    ConservativeCompactionPolicy,
    KeepAllCompactionPolicy,
    MessageCompactionDecision,
    MessageCompactionPolicy,
    SemanticToolCompactionPolicy,
)
from framework.memory.compression.importance import HeuristicImportanceScorer, ImportanceScorer
from framework.memory.compression.tool_chain_sanitizer import (
    DefaultSessionToolChainSanitizer,
    SessionToolChainSanitizer,
    ToolChainSanitizationIssue,
    ToolChainSanitizationMode,
    ToolChainSanitizationReason,
    ToolChainSanitizationResult,
)
from framework.memory.context_governance import (
    CompositeGovernance,
    ContextGovernance,
    MicrocompactGovernance,
    TokenBudgetGovernance,
    ToolChainRepairGovernance,
)
from framework.memory.core.consolidation import (
    ConsolidationEngine,
    ConsolidationResult,
    MemoryUpdate,
    MemoryUpdateMode,
)
from framework.memory.core.layers import (
    ArchiveMemoryManager,
    KnowledgeMemoryManager,
    MemoryLayerSet,
    PendingPrunedInputMemoryManager,
    SessionMemoryManager,
)
from framework.memory.core.models import (
    ArchiveEntry,
    CompressionPlan,
    CompressionReason,
    CompressionTrigger,
    KnowledgeBudget,
    LongTermMemory,
    MemoryBudget,
    MemoryContextBundle,
    PromptSection,
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
    PeerPairScope,
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
    InjectionFilterStrategy,
    MemoryInjectionPolicy,
    RestrictedInjectionPolicy,
    ToolMessageFilterStrategy,
)
from framework.memory.layers import (
    ArchiveMemoryConfig,
    KnowledgeMemoryConfig,
    MemoryLayerConfigSet,
    MemoryLayerFactory,
    PendingPrunedInputEntry,
    PendingPrunedInputMemoryConfig,
    ScopedArchiveMemoryManager,
    ScopedKnowledgeMemoryManager,
    ScopedPendingPrunedInputMemoryManager,
    ScopedSessionMemoryManager,
    SessionMemoryConfig,
)
from framework.memory.pending import (
    DefaultPendingPrunedInputExtractor,
    DefaultPendingPrunedInputInjector,
    PendingPrunedInputExtractor,
    PendingPrunedInputInjector,
)
from framework.memory.recorder import MemoryAppendRecorder, MemoryAppendSource
from framework.memory.registry import (
    DefaultMemoryStoreRegistry,
    InMemoryStoreRegistry,
    MemoryStoreRegistry,
)
from framework.memory.system import (
    MemorySystemContextManager,
    create_memory_system,
)

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
    "PendingPrunedInputMemoryManager",
    "PendingPrunedInputMemoryConfig",
    "PendingPrunedInputEntry",
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
    "PeerPairScope",
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
    "ScopedPendingPrunedInputMemoryManager",
    # Shared models
    "ArchiveEntry",
    "CompressionPlan",
    "CompressionReason",
    "CompressionCommitResult",
    "CompressionTrigger",
    "KnowledgeBudget",
    "MemoryBudget",
    "MemoryContextBundle",
    "PromptSection",
    "StorageRevision",
    # Shared models
    "LongTermMemory",
    # Recorder
    "MemoryAppendRecorder",
    "MemoryAppendSource",
    # Compression (strategies live in compression sub-package)
    "ImportanceScorer",
    "HeuristicImportanceScorer",
    # Tool-chain sanitizer
    "DefaultSessionToolChainSanitizer",
    "SessionToolChainSanitizer",
    "ToolChainSanitizationIssue",
    "ToolChainSanitizationMode",
    "ToolChainSanitizationReason",
    "ToolChainSanitizationResult",
    # Consolidation
    "ConsolidationEngine",
    "ConsolidationResult",
    "MemoryUpdate",
    "MemoryUpdateMode",
    # Archiving
    "ArchiveStrategy",
    "PreserveSummaryArchiveStrategy",
    "RawDumpArchiveStrategy",
    # Injection
    "MemoryInjectionPolicy",
    "FullInjectionPolicy",
    "RestrictedInjectionPolicy",
    "InjectionFilterStrategy",
    "ToolMessageFilterStrategy",
    # History search
    "HistorySearchStrategy",
    "KeywordHistorySearch",
    # Context governance
    "ContextGovernance",
    "CompositeGovernance",
    "ToolChainRepairGovernance",
    "MicrocompactGovernance",
    "TokenBudgetGovernance",
    "DefaultPendingPrunedInputExtractor",
    "DefaultPendingPrunedInputInjector",
    "PendingPrunedInputExtractor",
    "PendingPrunedInputInjector",
    # Auto compact (removed; use DefaultMemoryMaintenancePolicy directly)
    # Compaction policy / boundary (pipeline removed; workflow unified under coordinator)
    "MessageCompactionPolicy",
    "MessageCompactionDecision",
    "ConservativeCompactionPolicy",
    "KeepAllCompactionPolicy",
    "SemanticToolCompactionPolicy",
    "BoundaryPolicy",
    "ToolChainBoundaryPolicy",
    "UserTurnToolChainBoundaryPolicy",
]
