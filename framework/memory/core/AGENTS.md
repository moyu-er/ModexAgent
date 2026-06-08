<!-- Parent: ../AGENTS.md -->

# memory/core

Foundational memory abstractions — ABCs, Protocols, data models, and scope system. All memory layer implementations depend on these contracts.

## Key Files

| File | Description |
|------|-------------|
| `system.py` | `MemorySystem` ABC — CRUD + lifecycle + injection reads; `BudgetManagedMemorySystem` ABC; `ContextManagedMemorySystem` ABC |
| `storage.py` | `MemoryStorage` ABC — unified async interface for messages, logs, KV, cursors |
| `scope.py` | `MemoryScope` ABC + `SessionScope`, `UserScope`, `TenantScope`, `AgentScope`, `ChannelScope`, `ChatScope`, `PeerPairScope`, `CompositeScope`, `GlobalScope`; `MemoryContext` dataclass |
| `message.py` | `ChatMessage` (Pydantic model), `ContentFormat(StrEnum)` — PLAIN/XML |
| `models.py` | `LongTermMemory`, `MemoryContextDict` — data models |
| `layers.py` | `SessionMemoryManager`, `ArchiveMemoryManager`, `KnowledgeMemoryManager`, `UserRetentionBuffer` ABCs |
| `consolidation.py` | `MemoryUpdateMode` (StrEnum), `MemoryUpdate` (dataclass) — long-term memory update types |
| `lock.py` | `StorageLock` ABC — concurrency control |
| `__init__.py` | Re-exports key abstractions |

## Design Rules

- All storage operations are async.
- `MemoryScope.resolve(context)` returns a scope key for storage namespacing.
- `MemoryStorage` is the single persistence contract — all layer managers use it.
- `ChatMessage` uses Pydantic `extra='allow'` for forward compatibility.
