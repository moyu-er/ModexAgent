"""Core abstractions for the tiered memory system.

This package hosts the memory ABCs and shared models. The **split store**
ABCs (:class:`MessageStore`, :class:`KVStore`, :class:`CursorStore`,
:class:`ArchiveStore`) and the :class:`MemoryStoreBundle` composer are the
storage contract surface, exported below.
"""

from __future__ import annotations

from modex_agent.memory.core.provider import MemoryProvider
from modex_agent.memory.core.split_stores import (
    ArchiveStore,
    CursorStore,
    KVStore,
    MemoryStoreBundle,
    MessageStore,
)

__all__ = [
    "ArchiveStore",
    "CursorStore",
    "KVStore",
    "MemoryProvider",
    "MemoryStoreBundle",
    "MessageStore",
]
