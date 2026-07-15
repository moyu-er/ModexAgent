"""Physical store metadata ABC — locking and filesystem path.

Concrete store implementations (``DefaultScopedStorage``,
``DirArchiveStorage``, ``MarkdownKnowledgeStorage``,
``InMemoryScopedStorage``) inherit :class:`StoreMetadata` to expose two
capabilities that sit outside the data-access ABCs
(:class:`~modex_agent.memory.core.split_stores.MessageStore` etc.):

- ``get_lock()`` — the shared read/write lock used by layer managers for
  compound (multi-store) atomic operations within a
  :class:`~modex_agent.memory.core.split_stores.MemoryStoreBundle`.
- ``base_path`` — the filesystem directory backing the store (``None`` for
  non-file backends like in-memory).

Both are accessed by layer code via ``isinstance(store, StoreMetadata)`` at
the store-backend extension boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from modex_agent.memory.core.lock import StorageLock

__all__ = ["StoreMetadata"]


class StoreMetadata(ABC):
    """Physical metadata for a concrete store: lock and filesystem path.

    Layer managers receive a :class:`MemoryStoreBundle` whose store fields
    are typed as the split ABCs.  When a layer needs the lock (for compound
    read-modify-write transactions) or the filesystem path (for backup /
    archive directory resolution), it checks ``isinstance(store,
    StoreMetadata)`` and accesses the capability through this interface.
    """

    @abstractmethod
    def get_lock(self) -> StorageLock:
        """Return the shared read/write lock for this store instance."""
        ...

    @property
    @abstractmethod
    def base_path(self) -> Path | None:
        """Return the filesystem directory for this store, or ``None`` if not file-backed."""
        ...
