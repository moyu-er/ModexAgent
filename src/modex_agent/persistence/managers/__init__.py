"""Persistence lifecycle managers.

- :class:`WorkspacePersistenceManager` — opens/closes the per-workspace SQLite
  DB (``DatabaseKind.WORKSPACE``) and constructs DB-backed
  :class:`~modex_agent.memory.core.split_stores.MemoryStoreBundle` instances.
- :class:`RegistryPersistenceManager` — opens/closes the global registry
  SQLite DB (``DatabaseKind.REGISTRY``) at BotService initialize/stop.
"""

from __future__ import annotations

from modex_agent.persistence.managers.registry import RegistryPersistenceManager
from modex_agent.persistence.managers.workspace import WorkspacePersistenceManager

__all__ = [
    "RegistryPersistenceManager",
    "WorkspacePersistenceManager",
]
