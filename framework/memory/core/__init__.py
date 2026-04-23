"""Memory system core abstractions."""

from .base_managers import (
    BaseHistoryArchiveManager,
    BaseLongTermMemoryManager,
    BaseShortTermManager,
    BaseWorkingMemoryManager,
)
from .compression import CompressionContext, CompressionResult, CompressionStrategy
from .consolidation import ConsolidationEngine, ConsolidationResult, MemoryUpdate
from .message import ChatMessage
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

__all__ = [
    "BaseHistoryArchiveManager",
    "BaseLongTermMemoryManager",
    "BaseShortTermManager",
    "BaseWorkingMemoryManager",
    "ChatMessage",
    "CompressionResult",
    "CompressionStrategy",
    "CompressionContext",
    "ConsolidationEngine",
    "ConsolidationResult",
    "MemoryUpdate",
    "MemoryContext",
    "MemoryScope",
    "SessionScope",
    "UserScope",
    "TenantScope",
    "AgentScope",
    "ChannelScope",
    "ChatScope",
    "GlobalScope",
    "CompositeScope",
    "MemoryStorage",
]
