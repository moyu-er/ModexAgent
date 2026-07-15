# Tickets: Hybrid Persistence (SQLite + File)

One-line summary: Implement per-workspace SQLite persistence, ABC convergence, scope refactor, and bot SQLite backend selection.

Source spec: `docs/design/hybrid-persistence/PRD.md`
ADR: `docs/adr/0023-hybrid-persistence-sqlite-plus-file.md`

Work the **frontier**: any ticket whose blockers are all done. For a purely linear chain that means top to bottom. The initial frontier is: T01, T02, T12, T13, T15, T19. T03 is intentionally not on the initial frontier: its replacement interface fixtures arrive in T09 and T14.

---

## T01: `canonical_json` recursive deterministic serializer

**What to build:** A standalone utility function that recursively sorts dict keys at every nesting level, converts sets to sorted lists, preserves list element order while recursively canonicalizing each element. Uses `ensure_ascii=False`, compact separators. This is the foundation for all deterministic JSON output (RecordScope canonical form, DB scope_key uniqueness, payload comparisons).

**Blocked by:** None — can start immediately.

- [ ] `canonical_json(data: Any) -> str` implemented in `modex_agent/utils/canonical_json.py`
- [ ] Recursive: dict keys sorted, sets sorted→list, lists recursive, nested dicts sorted
- [ ] Mixed-type set sorting (None < bool < int/float < str < other)
- [ ] `ensure_ascii=False`, `separators=(",", ":")`
- [ ] NaN/Infinity rejected explicitly with `allow_nan=False`
- [ ] Unit tests: same semantic data → same bytes regardless of construction order; nested dicts; sets; lists; mixed types

---

## T02: SQLite connection management + migration system

**What to build:** `ConnectionManager` (open one aiosqlite connection, set PRAGMAs, coordinate every adapter operation and logical transaction with one async lock, close with wal_checkpoint TRUNCATE) + `MigrationRunner` (typed `DatabaseKind`, schema_migrations table, version-tracked SQL file runner) + empty migration directory structure. Applying one migration and inserting its version row is one explicit transaction; do not rely on `executescript()` as an implicit transaction boundary. After this, calling `ConnectionManager.open()` on a fresh path creates the correct DB kind, sets PRAGMAs, and runs zero migrations; `close()` does WAL checkpoint and closes cleanly.

**Blocked by:** None — can start immediately.

- [ ] `ConnectionManager` class with `open()` / `close()` plus typed query/execute/transaction operations; raw connection is private
- [ ] Closed `DatabaseKind` enum (`WORKSPACE`, `REGISTRY`) selects one migration stream
- [ ] Manager-owned async operation/transaction lock; adapters do not control the raw connection transaction independently
- [ ] Transaction contract forbids LLM/network/file I/O while held and does not support nesting in v1
- [ ] `MigrationRunner` class with `run_pending()` / `_current_version()` / `_apply()`
- [ ] `schema_migrations` table auto-created on first open
- [ ] Migration files discovered from packaged `migrations/{scope}/` directory
- [ ] Each migration's SQL statements + version row commit atomically and roll back together on failure
- [ ] Migration SQL rejects transaction-control statements; runner supplies `BEGIN IMMEDIATE`/commit/rollback
- [ ] `aiosqlite>=0.20.0` added to framework `pyproject.toml`
- [ ] Migration files packaged via hatch `force-include`
- [ ] PRAGMA settings applied on every connection
- [ ] `close()` runs `PRAGMA wal_checkpoint(TRUNCATE)` before `connection.close()`
- [ ] Tests: open→close→reopen works; migration idempotency (run twice); crash recovery (write without close, reopen, data intact)

---

## T03: Delete `InMemoryStorage` / `InMemoryStoreRegistry` / `InMemoryRegistryStore`

**What to build:** Remove exactly the three named test-only InMemory implementations from the framework layer. Their `scope_key` parameter mismatch with `DefaultScopedStorage` constrains the ABC design. Tests migrate first to the corresponding temporary file-backed interface fixtures produced by T09/T14; SQLite is added later through conformance fixtures. Do not delete `InMemoryInboxServer`, runtime in-memory fakes, or unrelated in-memory adapters.

**Blocked by:** T09 (file memory bundle fixture), T14 (file workspace-registry fixture).

- [ ] `InMemoryStorage` class deleted from `memory/stores/in_memory.py`
- [ ] `InMemoryStoreRegistry` class deleted from `memory/registry/in_memory.py`
- [ ] `InMemoryRegistryStore` class deleted from `workspace/registry.py`
- [ ] All `__init__.py` exports updated
- [ ] Tests using the memory classes migrated to temporary file-backed `MemoryStoreBundle` fixtures
- [ ] Tests using `InMemoryRegistryStore` migrated to temporary `WorkspaceRegistryStore` file fixtures
- [ ] No framework code references the three named InMemory classes
- [ ] `InMemoryInboxServer` and unrelated runtime test fakes remain available
- [ ] All tests pass

---

## T04: `RecordScope` model + `Scope` ABC refactor

**What to build:** `RecordScope` (frozen Pydantic model with all dimensional fields: pool, workspace_id, session_id, session_prefix, agent_id, agent_role, user_id, tenant_id, channel, chat_id, invocation_id, parent_session_id) with `canonical()` (uses `canonical_json`), `to_path_segment(*dimensions)`, `merge(other)`. New `Scope` ABC with `extract(context) -> RecordScope` replacing `MemoryScope.get_scope_key(context) -> str`. All concrete Scope subclasses rewritten. `CompositeScope` uses `RecordScope.merge()`. `PeerPairScope` removed from docs. Config `scope: str` → `scope: list[str]`. This is the expand phase — new `Scope` ABC coexists with old `MemoryScope`.

**Blocked by:** T01 (`canonical_json`)

- [ ] `RecordScope` Pydantic model with `canonical()`, `to_path_segment()`, `merge()`
- [ ] `Scope` ABC with `extract(context) -> RecordScope` and `name` property
- [ ] All 8 concrete Scope subclasses rewritten (`SessionScope`, `UserScope`, `TenantScope`, `AgentScope` with agent_role, `ChannelScope`, `ChatScope`, `GlobalScope`; `PeerPairScope` removed)
- [ ] `CompositeScope.extract()` uses `RecordScope.merge()` instead of colon-join
- [ ] Config `scope: str` → `scope: list[str]` with single-string auto-wrap
- [ ] `Scope` ABC and `MemoryScope` ABC coexist (expand phase)
- [ ] `build_scope(dims: list[str]) -> Scope` factory
- [ ] Tests: `canonical()` determinism; `merge()` field priority; `to_path_segment()` dimension ordering; all Scope subclasses extract correct fields from MemoryContext

---

## T05: Migrate all Scope call sites to new `Scope` ABC

**What to build:** Replace all `scope.get_scope_key(context)` calls with `scope.extract(context)` + `.canonical()` or `.to_path_segment()`. Remove the old `MemoryScope` ABC. This is the contract phase — old ABC deleted, only new `Scope` remains. All memory registry, store, and layer code uses `RecordScope` for both file path derivation and (future) DB scope columns.

**Blocked by:** T04 (`RecordScope` + `Scope` ABC)

- [ ] All `get_scope_key(context) -> str` calls replaced with `extract(context).canonical()` or `extract(context).to_path_segment(...)`
- [ ] `MemoryScope` ABC deleted
- [ ] `MemoryStoreRegistry` uses `Scope` ABC internally
- [ ] File path derivation uses `to_path_segment(*configured_dimensions)`
- [ ] All tests pass with new scope model
- [ ] No code references `get_scope_key` or `MemoryScope`

---

## T06: Workspace DB initial schema migration

**What to build:** `001_initial.sql` for the workspace DB creating all tables from SCHEMA-DESIGN.md: sessions, pool_routing, inbox_topics, inbox_messages, inbox_delivered_ids, inbox_dead_letter, turn_snapshots, approval_audit_log, todos, memory_session_messages (with state machine + CHECK constraints), memory_kv, memory_cursors, memory_revisions, memory_archive_state, memory_archive_entries, external_session_map, workspace_meta. Every table with a scope column has STORED generated columns + composite B-tree indexes. Partial unique index on turn_snapshots for one-active-turn. After `ConnectionManager.open()` on a fresh workspace DB, all tables exist with correct columns, constraints, and indexes.

**Blocked by:** T02 (ConnectionManager + MigrationRunner)

- [ ] `001_initial.sql` creates all 17 workspace tables
- [ ] STORED generated columns on all scope-bearing tables
- [ ] CHECK constraints (json_valid, enum values, state machine consistency)
- [ ] Partial unique index `idx_turn_active_unique` enforces one-active-turn
- [ ] Composite B-tree indexes on scope dimensions (pool+session, pool+agent, etc.)
- [ ] Foreign keys with correct ON DELETE strategies
- [ ] Migration applies cleanly on fresh DB
- [ ] Tests: table existence; generated column derivation; partial unique index rejects duplicate active turn; CHECK constraint rejects invalid state; json_valid rejects non-JSON scope

---

## T07: Registry DB initial schema migration

**What to build:** `001_initial.sql` for the registry DB creating `workspaces` (workspace_id, target_path UNIQUE, display_name, created_at, last_active, is_home, metadata_json) and `session_workspace_map` (session_prefix PK, workspace_id FK ON DELETE CASCADE). After `RegistryPersistenceManager.open()` on a fresh registry DB, both tables exist.

**Blocked by:** T02 (ConnectionManager + MigrationRunner)

- [ ] `001_initial.sql` creates `workspaces` and `session_workspace_map` tables
- [ ] `target_path` UNIQUE constraint
- [ ] `session_workspace_map.workspace_id` FK with ON DELETE CASCADE
- [ ] Index on `last_active` for recent-workspaces query
- [ ] Index on `workspace_id` for reverse lookup
- [ ] Migration applies cleanly on fresh DB
- [ ] Tests: table existence; UNIQUE on target_path; CASCADE on workspace delete

---

## T08: Define split memory store ABCs + `MemoryStoreBundle`

**What to build:** Define 4 new ABCs: `MessageStore` (load_messages, save_messages, append_message, get_revision, prune_messages, pin_message, unpin_message, delete_message, cleanup_expired), `KVStore` (get, set, delete, list_keys), `CursorStore` (get_last_cursor, set_last_cursor), `ArchiveStore` (append_log, read_logs, save_logs, read_archive_state, write_archive_state, append_channel_log, read_channel_logs, save_channel_logs, prune_to_max, cleanup_empty_dirs). Define `MemoryStoreBundle` (frozen Pydantic, arbitrary_types_allowed, messages/kv/cursors plus optional archive). This is the expand phase — new ABCs coexist with old `MemoryStorage`; zero call-site changes.

**Blocked by:** T04 (`RecordScope` for scope_key in method signatures)

- [ ] `MessageStore` ABC with all 9 methods (including state machine methods)
- [ ] `KVStore` ABC with 4 methods
- [ ] `CursorStore` ABC with 2 methods
- [ ] `ArchiveStore` ABC with 10 methods (3 log + 7 archive/channel/retention methods)
- [ ] `MemoryStoreBundle` Pydantic model (messages, kv, cursors, archive: ArchiveStore | None)
- [ ] `MemoryStorage` ABC still exists (expand phase, not yet deleted)
- [ ] No call sites changed yet
- [ ] `LogStore` concept does not exist as a separate ABC

---

## T09: File implementations refactor to implement split ABCs

**What to build:** `DefaultScopedStorage` simultaneously implements `MessageStore` + `KVStore` + `CursorStore` + `ArchiveStore` (one class, four interfaces). `DirArchiveStorage` implements `KVStore` + `ArchiveStore` + `CursorStore`. `MarkdownKnowledgeStorage` implements `KVStore` + `CursorStore`. State machine methods in file impl: `prune_messages` removes from messages.jsonl + returns pruned content; `pin_message`/`unpin_message` mark `_pinned: true` in message metadata; `cleanup_expired` is no-op (immediate physical delete, no soft_deleted). `MemoryStoreRegistry.resolve()` returns `MemoryStoreBundle` (file impl: all 4 fields point to same instance).

**Blocked by:** T08 (ABC definitions)

- [ ] `DefaultScopedStorage` implements all 4 ABCs
- [ ] `DirArchiveStorage` implements `KVStore` + `ArchiveStore` + `CursorStore`
- [ ] `MarkdownKnowledgeStorage` implements `KVStore` + `CursorStore`
- [ ] `prune_messages` returns `(count, pruned_messages_list)` in file impl
- [ ] `pin_message`/`unpin_message` use message dict metadata `_pinned: true`
- [ ] `cleanup_expired` is no-op in file impl
- [ ] `MemoryStoreRegistry.resolve()` returns `MemoryStoreBundle`
- [ ] File bundle: all fields point to same `DefaultScopedStorage` instance
- [ ] `MemoryStorage` ABC still exists but is no longer used by registry

---

## T10: Migrate memory layer 60 call sites to `MemoryStoreBundle`

**What to build:** Update all 60 call sites in `memory/layers/session.py`, `memory/layers/knowledge.py`, `memory/layers/archive.py`, `memory/layers/user_buffer.py`, `memory/lifecycle.py`, `memory/cleanup.py`, `memory/consolidation/dream_engine.py`, `memory/registry/` from `storage.xxx()` to `bundle.{messages|kv|cursors|archive}.xxx()`. Delete old `MemoryStorage` ABC. This is the contract phase — old ABC deleted, only split ABCs + bundle remain. CI green.

**Blocked by:** T09 (file implementations produce correct bundles), T03 (divergent InMemory subclass removed)

- [ ] All 60 `storage.xxx()` calls replaced with `bundle.{messages|kv|cursors|archive}.xxx()`
- [ ] Method routing: messages→MessageStore, get/set/delete/list_keys→KVStore, cursors→CursorStore, logs/archive→ArchiveStore
- [ ] `MemoryStorage` ABC deleted
- [ ] All memory layer tests pass
- [ ] No code references `MemoryStorage` type

---

## T11: `InboxMQ` ABC evolution + merge `DeliveredIdTracker`

**What to build:** Evolve `InboxServer` ABC to `InboxMQ` with new `deliver()` sync method for cross-process CLI use. Formalize topic lifecycle (pending→active→idle→expired) per PRD. Add `wakeup`/`wait_wakeup`/`reap_expired` methods. Merge `DeliveredIdTracker` into `InboxMQ` internal — delivered ID tracking is part of the inbox transaction, not a separate ABC. File implementation `LocalFileInboxMQ` adapts to new signature (deprecated, but functional for framework file-backend users).

**Blocked by:** T08 (ABC definition pattern as reference)

- [ ] `InboxMQ` ABC defined with `receive`/`consume`/`peek`/`count`/`clear`/`sessions_with_pending`/`deliver`/`wakeup`/`wait_wakeup`/`reap_expired`
- [ ] `deliver()` is sync (not async) — for CLI cross-process use
- [ ] SQLite `deliver()` contract owns a DB path and opens its own short-lived stdlib `sqlite3` connection; it never reuses the server's async connection
- [ ] `DeliveredIdTracker` ABC deprecated; delivered ID tracking is `InboxMQ` internal
- [ ] `LocalFileInboxMQ` (renamed from `LocalFileInboxServer`) adapts to new ABC
- [ ] `InboxServer` name kept as deprecated alias during transition
- [ ] Tests: file InboxMQ passes conformance with new ABC

---

## T12: `PoolRoutingStore` ABC extraction

**What to build:** Extract `PoolRoutingStore` ABC from the concrete `PoolSessionStore` class. ABC methods: `get_pool(session_prefix)`, `set_pool(session_prefix, pool_name)`, `delete_pool(session_prefix)`, `rename_pool(old, new)`, `list_prefixes()`. File implementation keeps existing behavior but implements the new ABC.

**Blocked by:** None — can start immediately.

- [ ] `PoolRoutingStore` ABC defined with 5 methods
- [ ] `PoolSessionStore` renamed to `LocalFilePoolRoutingStore` (or similar), implements ABC
- [ ] All callers updated to depend on `PoolRoutingStore` ABC, not concrete class
- [ ] Tests pass

---

## T13: `ExternalSessionMapStore` ABC extraction

**What to build:** Extract `ExternalSessionMapStore` ABC from the concrete `ExternalSessionStore` class. ABC methods: `resolve(modex_session_id) -> tuple[str | None, bool]`, `commit(modex_session_id, provider_session_id, provider_kind)`, `invalidate(modex_session_id)`. File implementation keeps existing behavior but implements the new ABC.

**Blocked by:** None — can start immediately.

- [ ] `ExternalSessionMapStore` ABC defined with 3 methods
- [ ] `ExternalSessionStore` renamed, implements ABC
- [ ] `ProviderKind` enum (pi, opencode) defined
- [ ] All callers updated to depend on ABC
- [ ] Tests pass

---

## T14: `WorkspaceRegistryStore` ABC deepening

**What to build:** Deepen `RegistryStore` to `WorkspaceRegistryStore` with new fields: `workspace_id` (UUID), `display_name`, `last_active`, `is_home`. Methods: `list_workspaces(order_by, limit)`, `upsert_workspace(record)`, `delete_workspace(target_path)`, `get_workspace(target_path)`. `WorkspaceRecord` Pydantic model. `RecentWorkspaces` (business layer) deprecated — `list_workspaces(order_by="last_active")` replaces it. File implementation `GlobalWorkspaceStore` adapts to new ABC (stores metadata in JSON instead of bare path list).

**Blocked by:** T04 (`RecordScope` for workspace identity)

- [ ] `WorkspaceRegistryStore` ABC defined with enriched methods
- [ ] `WorkspaceRecord` Pydantic model (workspace_id, target_path, display_name, created_at, last_active, is_home)
- [ ] `GlobalWorkspaceStore` adapts to new ABC (JSON metadata, not bare path list)
- [ ] `RecentWorkspaces` deprecated in business layer
- [ ] Tests pass

---

## T15: `SessionStore` remove `index_dir` parameter

**What to build:** Remove `index_dir: Path | None = None` parameter from all `SessionStore` ABC methods. Path is injected via constructor in file implementation, not passed per-call. This eliminates path leakage from the domain interface.

**Blocked by:** None — can start immediately.

- [ ] `index_dir` removed from all `SessionStore` ABC method signatures
- [ ] `LocalFileSessionStore` receives path via constructor
- [ ] All callers updated (path passed at construction, not per-call)
- [ ] Tests pass

---

## T16: `ApprovalAuditStore` ABC + SQLite adapter

**What to build:** New `ApprovalAuditStore` ABC with `record(entry)` and `query(session_id, since, limit)`. `ApprovalAuditEntry` frozen Pydantic model (turn_uuid, session_id, agent_id, turn_id, tool_name, tool_call_id, decision, deny_reason, decided_at, decided_by). SQLite adapter inserts into `approval_audit_log` (append-only). Add an internal workspace-persistence coordinator used by the approval decision handler to update the TurnSnapshot and append the audit entry under one `ConnectionManager.transaction()`. The two domain ABCs remain independently swappable; transaction orchestration does not leak SQL into them.

**Blocked by:** T02 (ConnectionManager transaction coordinator), T06 (workspace schema), T21 (SQLite TurnStateStore participates in the atomic decision write).

- [ ] `ApprovalAuditStore` ABC defined
- [ ] `ApprovalAuditEntry` Pydantic model
- [ ] `SqliteApprovalAuditStore` adapter (INSERT only, no UPDATE/DELETE)
- [ ] Approval decision handler calls `record()` on every approve/deny
- [ ] SQLite decision coordinator persists snapshot + audit row in one transaction with rollback on either failure
- [ ] `query()` returns entries filtered by session_id and/or timestamp
- [ ] Tests: record→query roundtrip; append-only (no update/delete); query by session

---

## T17: `SessionArtifactCleaner` ABC + default implementation

**What to build:** New `SessionArtifactCleaner` ABC coordinating DB row deletion + file directory deletion for session cascade cleanup. Method: `clean_session_artifacts(session_id, scope) -> SessionCleanupResult`. Default implementation: DB operations (DELETE from sessions, memory_session_messages, todos, turn_snapshots, inbox_messages, approval_audit_log WHERE session_id matches) + file operations (delete pruned, media, trace, output directories). `SessionGarbageCollector` (business layer) delegates to this ABC. Artifact list drops from 10 to 9 (fork_contexts removed).

**Blocked by:** T06 (workspace schema — need to know which tables to clean), T08 (ABC pattern)

- [ ] `SessionArtifactCleaner` ABC defined
- [ ] `SessionCleanupResult` Pydantic model (db_rows_deleted, files_deleted, dirs_deleted, errors)
- [ ] Default implementation: DB DELETE + file directory deletion
- [ ] `SessionGarbageCollector` (business layer) calls `SessionArtifactCleaner`
- [ ] Orphan artifact scanning routed through this ABC
- [ ] fork_contexts removed from artifact list (10 → 9)
- [ ] Tests: cascade delete removes DB rows + file dirs; orphan scan finds and cleans

---

## T18: `ContextForkBuilder` simplify to pure computation

**What to build:** `ContextForkBuilder.build()` no longer writes fork XML files. It queries the parent session's `MessageStore.load_messages()` for the last N active messages, applies lossy compaction, and returns the XML string directly. The in-memory cleanup registry (`_registry: dict[str, Path]`) and `cleanup()` method are removed. No file I/O. `register_for_cleanup()` becomes a no-op or is removed.

**Blocked by:** T09 (file implementations produce `MessageStore` interface)

- [ ] `build()` queries `MessageStore.load_messages()` instead of reading fork XML file
- [ ] No fork XML files written to disk
- [ ] `_registry` dict removed
- [ ] `cleanup()` method removed (or no-op)
- [ ] `register_for_cleanup()` removed (or no-op)
- [ ] `SessionGarbageCollector` artifact list updated (fork_contexts removed)
- [ ] Tests: fork context built from MessageStore; no files created; lossy compaction applied

---

## T19: Delete `JsonTerminalStateStore` dead code

**What to build:** Remove `JsonTerminalStateStore` class and the `save_state()`/`load_state()` path in `BaseTerminalManager`. These are never used in production (no wiring passes `storage_dir`). Remove the architecture guard test that checks for the save/load seam. `storage_dir` parameter and `_store` attribute removed from `BaseTerminalManager`.

**Blocked by:** None — can start immediately.

- [ ] `JsonTerminalStateStore` class deleted
- [ ] `BaseTerminalManager.save_state()` / `load_state()` deleted
- [ ] `storage_dir` parameter removed from `BaseTerminalManager.__init__`
- [ ] `_store` attribute removed
- [ ] Architecture guard test for save/load seam deleted
- [ ] All imports updated
- [ ] All tests pass

---

## T20: SQLite `InboxMQ` adapter + CLI `deliver()` integration

**What to build:** `SqliteInboxMQ` adapter implementing `InboxMQ` ABC through `ConnectionManager` for async methods; its sync `deliver()` is path-owned and opens a separate short-lived stdlib `sqlite3` connection for CLI use. `receive()` inserts into inbox_messages with UNIQUE(session_id, message_id) for idempotency. `consume()` atomically updates state to 'consumed' and records delivered_id in the same manager-owned transaction. `modexctl send` calls `deliver()`, which runs BEGIN IMMEDIATE, upserts topic + inserts message, commits, and closes. Bot's inbox switches from file to DB. This is the first end-to-end verifiable vertical slice: CLI sends message → DB inbox → poller consumes → agent turn starts.

**Blocked by:** T06 (workspace schema — inbox tables), T11 (InboxMQ ABC)

- [ ] `SqliteInboxMQ` adapter with all `InboxMQ` methods
- [ ] `receive()` idempotent via `ON CONFLICT(session_id, message_id) DO NOTHING`
- [ ] `consume()` atomic: UPDATE state + INSERT delivered_id in one transaction
- [ ] `deliver()` sync method using stdlib sqlite3 (for CLI)
- [ ] `modexctl send` uses `deliver()` with short-lived connection
- [ ] `MODEX_INBOX_ROOT` env var still works (CLI derives state.db from parent)
- [ ] `reap_expired()` deletes expired messages
- [ ] Bot IOC factory selects `SqliteInboxMQ` for `PersistenceBackend.SQLITE`
- [ ] Tests: receive→consume roundtrip; idempotent receive; CLI deliver; concurrent CLI + server; reap_expired

---

## T21: SQLite `TurnStateStore` adapter

**What to build:** `SqliteTurnStateStore` adapter implementing `TurnStateStore` ABC. Stores full TurnSnapshot as versioned JSON payload + indexed columns (agent_id, session_id, turn_id, phase, reason, timestamps). Partial unique index enforces one-active-turn. `save_turn()` INSERTs/UPDATEs within transaction. `find_active_turn()` uses partial index. `load_turn()` retrieves payload_json and deserializes. Bot's turn snapshots switch from JSON files to DB. Verifiable: suspend turn → kill process → restart → resume turn with approval.

**Blocked by:** T06 (workspace schema — turn_snapshots table)

- [ ] `SqliteTurnStateStore` adapter with save_turn/load_turn/find_active_turn/delete_turn/list_active_turns
- [ ] Partial unique index rejects second active turn for same (agent_id, session_id)
- [ ] TurnSnapshot serialized as versioned payload_json
- [ ] `ApprovalTransaction` stays inside payload_json (no separate table)
- [ ] Bot IOC factory selects `SqliteTurnStateStore` for `PersistenceBackend.SQLITE`
- [ ] Tests: save→load roundtrip; one-active-turn enforcement (concurrent insert rejected); suspend→restart→resume; completed turn cleanup

---

## T22: SQLite `SessionStore` + `PoolRoutingStore` adapters

**What to build:** `SqliteSessionStore` adapter (session metadata CRUD + parent-child graph queries using generated column indexes) + `SqlitePoolRoutingStore` adapter (session_prefix → pool routing). Bot's session index and pool routing switch from JSON files to DB. Session queries (list by prefix, find children, resolve by session_id) use DB indexes instead of directory scans. Pool routing writes are atomic (no more non-atomic `fp.write_text`).

**Blocked by:** T06 (workspace schema), T12 (PoolRoutingStore ABC), T15 (SessionStore without index_dir)

- [ ] `SqliteSessionStore` with get/save/list_by_prefix/get_children/delete
- [ ] `SqlitePoolRoutingStore` with get_pool/set_pool/delete_pool/rename_pool/list_prefixes
- [ ] `rename_pool` is a single UPDATE (atomic, vs current directory scan + per-file rewrite)
- [ ] Bot IOC factory selects SQLite adapters for `PersistenceBackend.SQLITE`
- [ ] Tests: session CRUD; parent-child graph; pool routing CRUD; rename_pool atomicity; pool routing corruption → explicit error (not silent default fallback)

---

## T23: SQLite `WorkspaceRegistryStore` adapter + registry DB integration

**What to build:** `SqliteWorkspaceRegistryStore` adapter using the registry DB. `RegistryPersistenceManager` opens at `BotService.initialize()`, closes at `stop()` (after all workspaces evicted). `WorkspaceRegistryStore.list_workspaces(order_by="last_active")` replaces `RecentWorkspaces`. WebUI recent-workspaces endpoint queries registry DB. Bot's workspace registry and session→workspace map switch from JSON files to registry DB.

**Blocked by:** T07 (registry schema), T14 (WorkspaceRegistryStore ABC)

- [ ] `SqliteWorkspaceRegistryStore` adapter with list_workspaces/upsert/delete/get
- [ ] `RegistryPersistenceManager` lifecycle: open at initialize, close at stop
- [ ] `RecentWorkspaces` business class removed; WebUI queries registry DB
- [ ] `session_workspace_map` table used for session→workspace routing
- [ ] Bot IOC factory wires `RegistryPersistenceManager`
- [ ] Tests: workspace upsert/list/delete; recent workspaces query; session→workspace map CRUD; registry DB lifecycle (open/close)

---

## T24: SQLite memory store adapters (MessageStore + KVStore + CursorStore + ArchiveStore)

**What to build:** 4 SQLite memory adapters implementing the split ABCs. `SqliteMessageStore` with state machine (normal/pinned/soft_deleted + TTL cleanup). `SqliteKVStore` with scope_key+key composite PK. `SqliteCursorStore` with scope_key+cursor_name PK. `SqliteArchiveStore` with archive_state + archive_entries tables, channel log methods. `WorkspacePersistenceManager` opens DB at workspace materialize, closes at evict. `MemoryStoreBundle` for DB backend: 4 fields point to 4 independent adapter instances (unlike file backend where all point to same instance). Bot's memory layer switches from files to DB. Verifiable: session messages written to DB → prune soft-deletes → archive generates Markdown → pruned catalog written to files.

**Blocked by:** T06 (workspace schema — memory tables), T09 (file implementations produce correct bundles — conformance reference), T10 (call sites use bundle, not MemoryStorage)

- [ ] `SqliteMessageStore` with load/save/append/get_revision + prune_messages (soft-delete + return content) + pin/unpin + delete_message + cleanup_expired (TTL physical delete)
- [ ] `SqliteKVStore` with get/set/delete/list_keys
- [ ] `SqliteCursorStore` with get_last_cursor/set_last_cursor
- [ ] `SqliteArchiveStore` with all 10 methods (3 log + 7 archive/channel/retention methods)
- [ ] `WorkspacePersistenceManager` lifecycle: open at materialize, close at evict (after pools/broker/terminals)
- [ ] `MemoryStoreRegistry.resolve()` returns DB-backed `MemoryStoreBundle`
- [ ] DB bundle: 4 fields point to 4 independent adapters
- [ ] `prune_messages` returns pruned content in same transaction as soft-delete
- [ ] Archive Markdown files remain on filesystem; only metadata in DB
- [ ] Tests: message state machine (normal→pinned→soft_deleted→DELETE); prune returns content; KV CRUD; cursor CRUD; archive channel log CRUD; TTL cleanup; bundle field independence

---

## T25: SQLite `ExternalSessionMapStore` + `TodoStore` adapters

**What to build:** `SqliteExternalSessionMapStore` adapter (resolve/commit/invalidate) + `SqliteTodoStore` adapter (per-session todo JSON). Bot's external session map and todos switch from JSON files to DB. External session map writes are atomic. Todo writes are atomic (single row upsert).

**Blocked by:** T06 (workspace schema), T13 (ExternalSessionMapStore ABC)

- [ ] `SqliteExternalSessionMapStore` with resolve/commit/invalidate
- [ ] `SqliteTodoStore` with get/save (upsert by session_id PK)
- [ ] Bot IOC factory selects SQLite adapters for `PersistenceBackend.SQLITE`
- [ ] Tests: external session resolve/commit/invalidate; todo save/load; invalidate → resolve returns (None, False)

---

## T26: Bot IOC config selects SQLite + lifecycle integration

**What to build:** Typed `PersistenceBackend` enum and `PersistenceConfig(backend=PersistenceBackend.SQLITE)` in bot IOC config. `WorkspacePersistenceManager` is attached to `PoolWorkspaceResources` (opened at materialize; at eviction, stop all DB-writing producers and complete final flushes before WAL checkpoint and close). `RegistryPersistenceManager` is attached to `BotService` (opened at initialize, closed at stop after all workspaces are evicted). Verifiable: bot starts → DBs open → normal operation → bot stops → WAL checkpoint → DBs close cleanly → restart → WAL replay → data intact.

**Blocked by:** T16 (Approval Audit), T17 (SessionArtifactCleaner), T18 (ContextForkBuilder), T19 (terminal state removal), T20 (InboxMQ SQLite), T21 (TurnState SQLite), T22 (Session+Pool SQLite), T23 (Registry SQLite), T24 (Memory SQLite), T25 (External+Todo SQLite).

- [ ] `PersistenceBackend` enum (`FILE`, `SQLITE`) and typed `PersistenceConfig.backend`
- [ ] Bot IOC config sets `backend=PersistenceBackend.SQLITE`
- [ ] `WorkspacePersistenceManager` on `PoolWorkspaceResources`, opened at materialize
- [ ] `RegistryPersistenceManager` on `BotService`, opened at initialize
- [ ] Per-workspace stop sequence: stop producers/pollers/pools/broker/terminals → final flushes → workspace DB close
- [ ] Global stop sequence: evict_all → registry DB close
- [ ] `ConnectionManager.close()` runs `PRAGMA wal_checkpoint(TRUNCATE)` before close
- [ ] Restart after crash: WAL auto-replay, committed data intact
- [ ] All IOC factories select SQLite adapters based on config
- [ ] End-to-end test: bot start → send message → turn → bot stop → restart → verify data

---

## T27: Conformance test suite

**What to build:** Parameterized conformance tests for every store ABC covering both `file` and `sqlite` backends. Same test, same assertions, two fixtures. SQLite-specific tests (not conformance): WAL multi-connection concurrency, partial unique index enforcement, generated column correctness, migration idempotency, crash recovery, cross-platform (Windows/macOS/Linux CI).

**Blocked by:** T16 (ApprovalAuditStore), T17 (SessionArtifactCleaner), T18 (ContextForkBuilder), T19 (terminal cleanup), T20 (InboxMQ SQLite), T21 (TurnState SQLite), T22 (Session+Pool SQLite), T23 (Registry SQLite), T24 (Memory SQLite), T25 (External+Todo SQLite).

- [ ] Conformance test base for each store ABC (InboxMQ, TurnStateStore, SessionStore, PoolRoutingStore, WorkspaceRegistryStore, ExternalSessionMapStore, TodoStore, MessageStore, KVStore, CursorStore, ArchiveStore, ApprovalAuditStore)
- [ ] `@pytest.fixture(params=["file", "sqlite"])` for each ABC
- [ ] SQLite-specific: WAL concurrency (framework + CLI simultaneous write)
- [ ] SQLite-specific: partial unique index (one-active-turn)
- [ ] SQLite-specific: generated column derivation from scope JSON
- [ ] SQLite-specific: migration idempotency (run twice)
- [ ] SQLite-specific: crash recovery (kill without close → reopen → data intact)
- [ ] SQLite-specific: cross-platform CI (Windows, macOS, Linux)
- [ ] All conformance tests pass on both backends

---

## T28: Documentation update

**What to build:** Update `memory/AGENTS.md`, `memory/core/AGENTS.md`, root `AGENTS.md` to reflect: ABC split (MemoryStorage → 4 ABCs + MemoryStoreBundle), InMemoryStorage deletion, DB as bot's backend (file remains as framework option), new ABCs (InboxMQ, PoolRoutingStore, ExternalSessionMapStore, WorkspaceRegistryStore, ApprovalAuditStore, SessionArtifactCleaner), ContextForkBuilder simplification, terminal state store removal, PersistenceConfig, scope system refactor.

**Blocked by:** T26 (bot integration complete), T27 (both backend contracts verified).

- [ ] `memory/AGENTS.md` updated (ABC split, bundle, no InMemoryStorage)
- [ ] `memory/core/AGENTS.md` updated (Scope ABC, RecordScope, no MemoryScope)
- [ ] Root `AGENTS.md` updated (new ABCs, persistence module, backend selection)
- [ ] `CONTEXT.md` verified current (terms added in earlier phase)
- [ ] `docs/adr/0023` status updated to "accepted" if implementation matches
- [ ] Stale references to `MemoryStorage`, `InboxServer`, `PoolSessionStore`, `ExternalSessionStore`, `RegistryStore`, `DeliveredIdTracker`, `JsonTerminalStateStore` removed from all docs

---

## T29: Minimal SQLite production-wiring closure

**Status:** Complete (2026-07-15).

**What was closed:** Connect the already-implemented SQLite adapters to the
`examples/bot_project` production lifecycle without changing the hybrid storage
boundary. This ticket closes wiring and ownership gaps discovered after T26; it
does not add schema or migrate existing file data.

**Blocked by:** T16 (approval audit + decision coordinator), T22 (session and
pool-routing adapters), T23 (registry adapter), T26 (backend selection and
lifecycle).

- [x] Workspace registry uses the configured `WorkspaceRegistryStore` backend;
  the shared interface is async, startup explicitly loads persisted contexts,
  and SQLite upsert preserves immutable `workspace_id` / `created_at` identity.
- [x] Registry DB uses the canonical
  `<home>/.modex/_registry/state.db` location and the registry migration stream;
  no lookup, copy, or rename of the former `registry.db` path is performed.
- [x] Each workspace session index is selected through `build_session_store()`:
  SQLite uses that workspace's `state.db`, FILE retains
  `WorkspacePoolSessionStore`, and the in-memory registry loads persisted
  sessions before pools accept work.
- [x] The service owns one home `WorkspacePersistenceManager` and one shared
  `SqlitePoolRoutingStore` in `<home>/.modex/state.db`; home resources borrow the
  manager, non-home resources own one manager each, and every workspace router
  receives the same cross-workspace routing store.
- [x] Live SQLite approval decisions use `SqliteDecisionCoordinator` so the
  updated `TurnSnapshot` and append-only `ApprovalAuditEntry` commit in one
  transaction. `turn_uuid` is part of the persisted ReAct snapshot payload,
  the coordinator rejects audit identities that differ from the snapshot before
  opening a transaction, decision scope uses `RecordScope.canonical()`, and
  FILE keeps its existing turn-store-only behavior.
- [x] Initialization rollback and normal shutdown close workspace writers first,
  then the shared routing store and home workspace manager, with registry
  persistence last. Failed or cancelled agent shutdown retains the agent and
  materialized workspace for retry, leaves owned SQLite managers open, and
  surfaces `BotServiceShutdownIncompleteError` instead of reporting successful
  shutdown. Failed non-home materialization closes only resources it owns.
- [x] Production WebUI session lookup resolves the materialized workspace's
  backend-selected `SessionStore`; it does not reconstruct a file session index
  while SQLite is selected.

**Explicit exclusions:** no existing file-to-SQLite data migration, no dual
write or shadow read, no database per pool, no new schema, and no conversion of
intentional file stores (knowledge/archive Markdown, pruned history, media,
overflow chunks, experience trees, traces, transcripts, configuration, prompts,
or skills).

**Verification evidence:** registry-focused suite `117 passed`; bot backend
suite `995 passed, 1 skipped, 13 deselected`; framework persistence/workspace
slice `907 passed`; approval-focused suites `28 passed`, `71 passed`, and bot
approval regression `27 passed`. Real SQLite drivers verified registry reopen,
session + routing persistence, and audit retention after turn-snapshot deletion.
The supported framework unit/framework/conformance boundary passed
`4449 passed, 18 skipped`; the final approval, pool ownership, workspace
retention, service lifecycle, and multi-live regression gate passed `83 passed`
(`1 deselected`). LSP error diagnostics are clean on the changed production
files, scoped Ruff and mypy are clean for the final coordinator/type fixes, and
`git diff --check` reports no whitespace errors. Final independent architecture
review found no Critical or High findings.

The combined repository + bot-project collection is not a valid final gate in
the current environment: pytest 9 rejects the existing nested
`tests/integration/bot_project/conftest.py::pytest_plugins`, and
`examples/bot_project/tests/test_policy.py` cannot resolve the existing
`plugins.tool_call_cleanup.policy` import. Repository-wide mypy and broad Ruff
also retain pre-existing debt outside this ticket (`398` mypy errors across
`97` files and `23` broad Ruff findings in the inspected wiring surface).
