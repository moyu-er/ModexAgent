"""Memory system core abstractions."""

from .consolidation import ConsolidationEngine, ConsolidationResult, MemoryUpdate
from .layers import (
    ArchiveMemoryManager,
    KnowledgeMemoryManager,
    MemoryLayerSet,
    SessionMemoryManager,
)
from .message import ChatMessage
from .models import (
    ArchiveEntry,
    CompressionPlan,
    CompressionReason,
    CompressionTrigger,
    MemoryBudget,
    MemoryContextBundle,
    PromptSection,
    StorageRevision,
    UnprocessedResult,
)
from .scope import (
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
from .storage import MemoryStorage
from .system import MemorySystem

__all__ = [
    "ChatMessage",
    "ArchiveEntry",
    "ArchiveMemoryManager",
    "CompressionPlan",
    "CompressionReason",
    "CompressionTrigger",
    "ConsolidationEngine",
    "ConsolidationResult",
    "MemoryUpdate",
    "MemoryBudget",
    "MemoryContextBundle",
    "MemoryContext",
    "MemoryScope",
    "PromptSection",
    "SessionScope",
    "UserScope",
    "TenantScope",
    "AgentScope",
    "ChannelScope",
    "ChatScope",
    "GlobalScope",
    "CompositeScope",
    "MemoryStorage",
    "MemorySystem",
    "KnowledgeMemoryManager",
    "MemoryLayerSet",
    "SessionMemoryManager",
    "StorageRevision",
    "UnprocessedResult",
]
