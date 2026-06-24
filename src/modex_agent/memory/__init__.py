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

from modex_agent.memory.core.scope import (
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
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.core.system import MemorySystem as MemorySystemABC
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.history import ListMessageHistory, MessageHistory
from modex_agent.memory.injection import (
    FullInjectionPolicy,
    MemoryInjectionPolicy,
    RestrictedInjectionPolicy,
)
from modex_agent.memory.layers import (
    ArchiveMemoryConfig,
    KnowledgeMemoryConfig,
    MemoryLayerConfigSet,
    MemoryLayerFactory,
    SessionMemoryConfig,
)
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.system import (
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
