"""Tiered memory system for agent context management.

Slim public facade. Callers reach the memory seam through a small set of
high-leverage names; everything else is an implementation detail that lives in
its own submodule and should be imported explicitly from there.

Public surface:
- Entry points: MemorySystem / MemorySystemABC, DefaultMemorySystem,
  create_memory_system, MemorySystemContextManager
- Context dimensions: MemoryContext and the concrete scope classes
- Injection policies: MemoryInjectionPolicy and its Full/Restricted variants
- Construction-time history data structures: ListMessageHistory, MessageHistory
- Layer configuration: MemoryLayerConfigSet, MemoryLayerFactory and the three
  layer config dataclasses
- Pruned catalog: PrunedManager
"""

from framework.memory.core.scope import (
    AgentScope,
    ChannelScope,
    ChatScope,
    CompositeScope,
    GlobalScope,
    MemoryContext,
    SessionScope,
    TenantScope,
    UserScope,
)
from framework.memory.core.system import MemorySystem
from framework.memory.core.system import MemorySystem as MemorySystemABC
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.history import ListMessageHistory, MessageHistory
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
    SessionMemoryConfig,
)
from framework.memory.pruned.manager import PrunedManager
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
    # Context dimensions
    "MemoryContext",
    "SessionScope",
    "UserScope",
    "TenantScope",
    "AgentScope",
    "ChannelScope",
    "ChatScope",
    "CompositeScope",
    "GlobalScope",
    # Injection policies
    "MemoryInjectionPolicy",
    "FullInjectionPolicy",
    "RestrictedInjectionPolicy",
    # History data structures
    "ListMessageHistory",
    "MessageHistory",
    # Layer configuration
    "MemoryLayerConfigSet",
    "MemoryLayerFactory",
    "SessionMemoryConfig",
    "ArchiveMemoryConfig",
    "KnowledgeMemoryConfig",
    # Pruned catalog
    "PrunedManager",
]
