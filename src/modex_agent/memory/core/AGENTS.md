<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-15 -->

# memory/core

Foundational memory abstractions: ABCs, data models, and the split-store
contract. All memory layer implementations depend on these contracts. The scope
system lives in `modex_agent.core.scope` (see below); it was promoted out of
this package during the hybrid-persistence refactor (T04/T05).

## Key Files

| File | Description |
|------|-------------|
| `system.py` | `MemorySystem` ABC, `BudgetManagedMemorySystem` ABC, `ContextManagedMemorySystem` ABC |
| `split_stores.py` | The four split store ABCs and `MemoryStoreBundle` (see below) |
| `storage.py` | Empty placeholder. The legacy `MemoryStorage` god-interface was removed in T10; kept only so stale imports raise a clear `ImportError` |
| `store_metadata.py` | `StoreMetadata` ABC, physical-store metadata (`get_lock()`, `base_path`) accessed via `isinstance` at the store-backend extension boundary |
| `layers.py` | `SessionMemoryManager`, `ArchiveMemoryManager`, `KnowledgeMemoryManager`, `UserRetentionBuffer` ABCs |
| `models.py` | `LongTermMemory`, `MemoryContextDict`, `StorageRevision`, `ArchiveEntry`, `UnprocessedResult` data models |
| `consolidation.py` | `MemoryUpdateMode` (StrEnum), `MemoryUpdate` dataclass, long-term memory update types |
| `lock.py` | `StorageLock` ABC, `AioRWLock`, `NoOpStorageLock` concurrency control |
| `__init__.py` | Re-exports the split store ABCs and `MemoryStoreBundle` |

### Split store contract (T08/T10)

The single `MemoryStorage` god-interface was split into four focused ABCs,
composed by `MemoryStoreBundle`:

- `MessageStore` (9 methods): `load_messages`, `save_messages`, `append_message`, `get_revision`, `prune_messages`, `pin_message`, `unpin_message`, `delete_message`, `cleanup_expired`
- `KVStore` (4 methods): `get`, `set`, `delete`, `list_keys`
- `CursorStore` (2 methods): `get_last_cursor`, `set_last_cursor`
- `ArchiveStore` (10 methods): `append_log`, `read_logs`, `save_logs`, `read_archive_state`, `write_archive_state`, `append_channel_log`, `read_channel_logs`, `save_channel_logs`, `prune_to_max`, `cleanup_empty_dirs`

`MemoryStoreBundle` is a frozen Pydantic model (`arbitrary_types_allowed=True`)
holding the three required stores (`messages`, `kv`, `cursors`) plus an optional
`archive` (`None` for sessions without archival history, e.g. ephemeral
subagent sessions). The bundle is what `MemoryStoreRegistry.resolve()` returns
and what every layer manager consumes.

## Scope system (`modex_agent.core.scope`)

The scope system was promoted to `modex_agent.core.scope` during T04/T05. The
old `MemoryScope` ABC and `get_scope_key()` are deleted. The new contract:

- `Scope` ABC: `extract(context: MemoryContext) -> RecordScope` replaces the
  old `MemoryScope.get_scope_key(context) -> str`. Each concrete subclass
  extracts a `RecordScope` from the context.
- `RecordScope`: frozen Pydantic model carrying every configurable isolation
  dimension (`pool`, `workspace_id`, `session_id`, `session_prefix`, `agent_id`,
  `agent_role`, `user_id`, `tenant_id`, `channel`, `chat_id`, `invocation_id`,
  `parent_session_id`). Field names are canonical across Python and SQL
  generated-column extraction.
  - `canonical()` produces a deterministic JSON string (via `canonical_json`)
    for DB `scope_key` uniqueness.
  - `to_path_segment(*dimensions)` derives file-path segments for file-backed
    stores (`None` values render as `"default"`).
  - `merge(other)` combines two records (`other`'s non-`None` fields override),
    used by `CompositeScope.extract`.
- `build_scope(dims: list[str] | str) -> Scope`: factory turning dimension
  short-names into a `Scope`. A single string auto-wraps into a one-element
  list; empty list returns `GlobalScope`; multiple dimensions return
  `CompositeScope` preserving order.
- Concrete subclasses: `SessionScope`, `UserScope`, `TenantScope`,
  `AgentScope`, `ChannelScope`, `ChatScope`, `GlobalScope`, `CompositeScope`.
  `PeerPairScope` was removed (T04); it was only documented, never implemented.
- `MemoryContext` lives here too (the context the scopes extract from).
- Config accepts `scope: list[str]` (not `str`); a single string is
  auto-wrapped by `build_scope`.

`CompositeScope` remains for file-backed stores (flat path segment). DB-backed
stores use `RecordScope.canonical()` instead. The two scope representations
coexist by design.

## Design Rules

- All storage operations are async.
- `Scope.extract(context)` returns a `RecordScope`; consumers derive either a
  DB key via `.canonical()` or a filesystem path via
  `scope_path_key(scope, context)`.
- The four split store ABCs are the persistence contract. Layer managers
  receive a `MemoryStoreBundle` and route data through
  `bundle.{messages|kv|cursors|archive}`.
- `ChatMessage` (in `modex_agent.core.message`) uses Pydantic `extra='allow'`
  for forward compatibility.
