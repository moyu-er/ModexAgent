"""Default memory layer implementations for the registry-based architecture."""

from framework.memory.layers.archive import ScopedArchiveMemoryManager
from framework.memory.layers.config import (
    ArchiveMemoryConfig,
    KnowledgeMemoryConfig,
    MemoryLayerConfigSet,
    PendingPrunedInputMemoryConfig,
    SessionMemoryConfig,
    StorageFactory,
)
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.layers.knowledge import ScopedKnowledgeMemoryManager
from framework.memory.layers.pending import PendingPrunedInputEntry, ScopedPendingPrunedInputMemoryManager
from framework.memory.layers.session import ScopedSessionMemoryManager

__all__ = [
    "ArchiveMemoryConfig",
    "KnowledgeMemoryConfig",
    "MemoryLayerConfigSet",
    "MemoryLayerFactory",
    "PendingPrunedInputEntry",
    "PendingPrunedInputMemoryConfig",
    "ScopedArchiveMemoryManager",
    "ScopedKnowledgeMemoryManager",
    "ScopedPendingPrunedInputMemoryManager",
    "ScopedSessionMemoryManager",
    "SessionMemoryConfig",
    "StorageFactory",
]
