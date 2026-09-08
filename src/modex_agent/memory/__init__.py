"""Tiered memory system for agent context management.

Slim public facade. Callers reach the memory seam through a small set of
high-leverage names; everything else is an implementation detail that lives in
its own submodule and should be imported explicitly from there.

Public surface:
- Entry points: MemorySystem, DefaultMemorySystem,
  create_memory_system, MemorySystemContextManager
- Context management: ContextManager, ContextState, InMemoryContextManager
- Context dimensions: MemoryContext and the concrete scope classes
- Injection policies: MemoryInjectionPolicy and its Full/Restricted variants
- Concrete histories: ListMessageHistory, ScopedMessageHistory
- Layer configuration: MemoryLayerConfigSet, MemoryLayerFactory and the three
  layer config dataclasses
- Pruned catalog: PrunedManager
"""

from modex_agent.memory.context import ContextManager, ContextState, InMemoryContextManager
from modex_agent.memory.core.system import MemorySystem
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.history import ListMessageHistory, ScopedMessageHistory
from modex_agent.memory.injection import (
    FullInjectionPolicy,
    MemoryInjectionPolicy,
    RestrictedInjectionPolicy,
)
from modex_agent.memory.layers import (
    ArchiveMemoryConfig,
    CoreMemoryConfig,
    MemoryLayerConfigSet,
    MemoryLayerFactory,
    SessionMemoryConfig,
)
from modex_agent.memory.pruned.manager import PrunedManager
from modex_agent.memory.scope import (
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
from modex_agent.memory.system import (
    MemorySystemContextManager,
    create_memory_system,
)

__all__ = [
    # Entry points
    "MemorySystem",
    "DefaultMemorySystem",
    "create_memory_system",
    "MemorySystemContextManager",
    # Context management
    "ContextManager",
    "ContextState",
    "InMemoryContextManager",
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
    "ScopedMessageHistory",
    # Layer configuration
    "MemoryLayerConfigSet",
    "MemoryLayerFactory",
    "SessionMemoryConfig",
    "ArchiveMemoryConfig",
    "CoreMemoryConfig",
    # Pruned catalog
    "PrunedManager",
]
