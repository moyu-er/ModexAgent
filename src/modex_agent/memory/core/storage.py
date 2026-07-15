"""Legacy MemoryStorage ABC — removed in T10 (contract phase).

The god-interface :class:`MemoryStorage` was split into four focused ABCs
(:class:`~modex_agent.memory.core.split_stores.MessageStore`,
:class:`~modex_agent.memory.core.split_stores.KVStore`,
:class:`~modex_agent.memory.core.split_stores.CursorStore`,
:class:`~modex_agent.memory.core.split_stores.ArchiveStore`) composed by
:class:`~modex_agent.memory.core.split_stores.MemoryStoreBundle`.

All memory layer code now receives a ``MemoryStoreBundle`` from the registry
and routes data access through ``bundle.{messages|kv|cursors|archive}``.

This module is kept as an empty placeholder so that stale imports produce a
clear ``ImportError`` rather than a silent ``ModuleNotFoundError``.
"""
