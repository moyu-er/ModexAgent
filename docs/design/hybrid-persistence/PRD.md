Status: ready-for-agent

# Hybrid Persistence: Per-Workspace SQLite + File Layer

## Problem Statement

ModexAgent's `.modex/` directory stores all persistent state as local files
(JSON, JSONL, Markdown, raw bytes). This works for single-process operation
but has structural problems that cannot be fixed without changing the
persistence substrate:

- **Inbox atomicity gap**: The framework's `InboxServer` uses a process-local
  `asyncio.Lock`; `modexctl` CLI uses an unrelated `FileLock` and appends
  directly to the same `pending.jsonl`. The two writers share no transaction
  boundary. `consume()` rewrites pending content before persisting delivered
  IDs — a crash between those steps loses a message without recording delivery.
- **Turn snapshot race**: `JsonFileTurnStateStore` does a scan-then-write
  active-turn check without a lock. Two writers can pass the check; a crash
  can leave a partial snapshot.
- **Session index scans**: `LocalFileSessionStore` recursively searches for a
  sanitized filename. Duplicate names under pool directories are ambiguous;
  parent/child queries require full directory scans.
- **Scope key instability**: `CompositeScope` joins dimensions with `":"`
  into a flat path segment — irreversible (values containing `:` are
  ambiguous), prefix-only matching, compile-time-hardcoded combinations.
- **God interface**: `MemoryStorage` carries message CRUD, KV, logs, cursors,
  and archive extensions in one ABC. A DB implementation must implement all
  five concerns in one class.
- **No approval audit trail**: Approval decisions live only inside
  `TurnSnapshot` and are overwritten by the next turn — compliance gap.
- **Dead code**: `JsonTerminalStateStore` is never used in production;
  `ContextForkBuilder` writes fork XML files that could be computed on-demand
  from the session message store.

## Solution

Adopt a **hybrid persistence architecture**: per-workspace SQLite databases for
transactional structured state, files for human-editable documents and binary
assets. Reorganize the persistence ABCs into single-responsibility interfaces
with both file and SQLite implementations. Refactor the scope system from
string-join to structured `RecordScope` with STORED generated columns for
true composite B-tree indexing.

No data migration from existing files — the system starts fresh with SQLite;
old file-based data is not imported (out of scope per user decision).

## User Stories

### Scope and Identity

1. As a framework developer, I want a structured `RecordScope` Pydantic model
   that carries all dimensional fields (pool, workspace_id, session_id,
   session_prefix, agent_id, agent_role, user_id, tenant_id, channel,
   chat_id, invocation_id, parent_session_id), so that scope dimensions are
   data, not compiled-away string joins.

2. As a framework developer, I want `RecordScope.canonical()` to produce a
   deterministic JSON string using recursive key sorting (dict keys sorted at
   every nesting level, sets sorted and converted to lists, lists preserve
   element order with recursive canonicalization), so that the same semantic
   scope always produces the same byte sequence for DB uniqueness columns.
   Non-finite floats are rejected (`allow_nan=False`) rather than serialized as
   JavaScript extensions.

3. As a framework developer, I want `RecordScope.to_path_segment(*dimensions)`
   to derive file-path segments for file-backed stores, so that file and DB
   stores share one scope model but produce different physical representations.

4. As a framework developer, I want `Scope.extract(context) -> RecordScope`
   to replace `MemoryScope.get_scope_key(context) -> str`, so that scope
   extraction produces structured data instead of an irreversible flat string.

5. As a framework developer, I want `CompositeScope` to use
   `RecordScope.merge()` (field-level merge, other-priority) instead of
   colon-string-join, so that combinations are reversible and support any
   dimension for querying.

6. As a framework developer, I want `PeerPairScope` removed (it was documented
   but never implemented; no peer-pair memory isolation scenario exists), so
   that the scope system has no phantom implementations.

7. As a framework developer, I want the `Scope` ABC to replace `MemoryScope`,
   with `extract(context) -> RecordScope` as the sole method, so that all
   scope implementations produce the same structured output.

8. As a framework developer, I want `AgentScope` to extract both `agent_id`
   and `agent_role` (not just `agent_id`), so that role-based memory isolation
   works correctly.

9. As a framework developer, I want config to express scope combinations as a
   list (`scope: ["tenant", "user"]`) rather than a single string
   (`scope: "user"`), so that combinations are expressible without code changes.

10. As a framework developer, I want `sender_agent` and `receiver_agent` to
    remain in `MemoryContext` for `infer_agent_role()` but NOT appear in
    `RecordScope`, so that they serve role inference without polluting the
    scope model.

### Database Architecture

11. As a bot operator, I want each workspace to have its own SQLite database
    file (`<workspace>/.modex/state.db`), so that workspace deletion, backup,
    and portability are file-level operations.

12. As a bot operator, I want a small global registry database
    (`<home>/.modex/_registry/state.db`) for cross-workspace routing only, so
    that workspace switching and recent-workspace queries are fast.

13. As a framework developer, I want pools to NOT have separate databases —
    pool is a dimension inside the workspace DB, not a partition — so that
    cross-pool session trees and peer messaging use cross-pool queries in one
    DB without ATTACH.

14. As a framework developer, I want the `scope` column to be a JSON object
    that is the sole source the application writes for generated dimensions,
    with STORED columns deriving those dimensions via
    `json_extract(scope, '$.key')`, so that true B-tree composite indexes work
    on any dimension combination. Ordinary domain keys and payload columns are
    still written normally.

15. As a framework developer, I want adding a new scope dimension to be
    `ALTER TABLE ADD COLUMN ... GENERATED ALWAYS AS ...` + `CREATE INDEX`
    with no application write-path change, so that the splitting criteria can
    evolve without rewriting store adapters.

16. As a framework developer, I want every DB connection to set
    `journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`,
    `busy_timeout=5000`, `wal_autocheckpoint=1000`, so that crash recovery,
    referential integrity, and multi-process access work correctly.

17. As a framework developer, I want SQLite and PostgreSQL to use equivalent
    operations (`json_extract` vs `->>`, `GENERATED ALWAYS AS ... STORED`,
    partial indexes, `ON CONFLICT DO NOTHING`), so that future PostgreSQL
    migration is mechanical.

18. As a bot operator on Windows, I want SQLite WAL mode and generated columns
    to work correctly, so that the persistence layer is cross-platform
    (Windows/macOS/Linux) with no platform-specific code.

19. As a framework developer, I want `aiosqlite>=0.20.0` as the async SQLite
    wrapper (not SQLAlchemy, not Alembic), so that the framework stays
    dependency-light with no ORM leakage.

20. As a CLI developer, I want `modexctl` to use stdlib `sqlite3` (no
    `aiosqlite`), so that the CLI has zero new dependencies and uses
    short-lived synchronous connections. `SqliteInboxMQ.deliver()` owns the DB
    path and opens that short-lived connection; it never drives the server's
    long-lived async connection from synchronous code.

### Store ABC Convergence

21. As a framework developer, I want `MemoryStorage` split into four
    single-responsibility ABCs: `MessageStore`, `KVStore`, `CursorStore`,
    `ArchiveStore`, so that each DB adapter implements one concern, not five.

22. As a framework developer, I want `LogStore` cancelled — archive channel
    logs are carried by the `ArchiveStore` DB table — so that there is no
    redundant log ABC.

23. As a framework developer, I want `DeliveredIdTracker` merged into
    `InboxMQ` internal (not an independent ABC), so that delivered ID
    tracking is part of the inbox transaction, not a separate call sequence
    that can race.

24. As a framework developer, I want `RegistryStore` deepened to
    `WorkspaceRegistryStore` with `workspace_id`, `target_path`,
    `display_name`, `last_active`, `is_home` fields, so that workspace
    metadata is queryable and `RecentWorkspaces` is absorbed.

25. As a framework developer, I want `RecentWorkspaces` (business layer)
    deprecated — `WorkspaceRegistryStore.list_workspaces(order_by=last_active)`
    replaces it — so that there is one source of truth for workspace
    recency.

26. As a framework developer, I want `PoolSessionStore` (concrete class)
    extracted to `PoolRoutingStore` ABC, so that DB and file implementations
    share one interface.

27. As a framework developer, I want `ExternalSessionStore` (concrete class)
    extracted to `ExternalSessionMapStore` ABC, so that DB and file
    implementations share one interface.

28. As a framework developer, I want `InboxServer` evolved to `InboxMQ` with
    a `deliver()` sync method for cross-process CLI use, so that `modexctl`
    and the framework server share one inbox write contract.

29. As a framework developer, I want `MemoryStoreBundle` (frozen Pydantic
    model with `arbitrary_types_allowed=True`) returned by
    `MemoryStoreRegistry.resolve()`, holding `MessageStore`, `KVStore`,
    `CursorStore`, and optional `ArchiveStore`, so that the registry returns
    a typed bundle, not a god interface.

30. As a framework developer, I want `SessionStore` to remove the
    `index_dir: Path` parameter from its ABC methods, so that path leakage
    does not appear in the domain interface.

31. As a framework developer, I want `PrunedStorage` to remain file-only
    (no SQLite adapter), so that agents can use file tools to browse pruned
    history.

32. As a framework developer, I want `MediaStore` to remain file-only (no
    SQL migration), so that binary streaming semantics are preserved.

33. As a framework developer, I want `ToolOverflowStore` to remain file-only,
    so that large text chunk streaming is preserved.

34. As a framework developer, I want `TraceStore` marked DB-optional, so that
    JSONL trace can continue until profiling justifies a telemetry DB.

35. As a framework developer, I want `ExperienceMetaStore` marked DB-optional,
    so that its migration is deferred.

### Session Message Lifecycle

36. As a framework developer, I want session messages to have a three-state
    lifecycle (`normal` → `pinned` → `soft_deleted`), so that pin protects
    important messages from auto-prune and soft-delete preserves content for
    TTL recovery.

37. As a framework developer, I want a `CHECK` constraint enforcing
    `(state = 'soft_deleted') = (deleted_at IS NOT NULL)`, so that the
    database guarantees `deleted_at` is only set for soft-deleted rows.

38. As a framework developer, I want `prune_messages()` to return the pruned
    message content list in the same transaction as the soft-delete, so that
    archive/pruned/URB consumers receive content without a separate query.

39. As a framework developer, I want a background TTL job that physically
    deletes `soft_deleted` rows past their retention window in batches, so
    that the DB does not grow unbounded.

40. As a framework developer, I want `pin_message()` / `unpin_message()` to
    toggle between `normal` and `pinned`, so that important messages survive
    auto-prune.

41. As a framework developer, I want `load_messages()` to return only
    `normal` + `pinned` (via a partial index `WHERE state IN ('normal',
    'pinned')`), so that soft-deleted messages are invisible to active
    queries.

### Approval Audit

42. As a framework developer, I want an append-only `approval_audit_log`
    table recording every approve/deny decision with `turn_uuid`,
    `session_id`, `tool_name`, `tool_call_id`, `decision`, `deny_reason`,
    `decided_at`, `decided_by`, so that approval history is not lost when
    `TurnSnapshot` is overwritten.

43. As a framework developer, I want an `ApprovalAuditStore` ABC with
    `record()` and `query()` methods, so that audit logging is behind a
    swappable interface.

    **Atomicity clarification:** the SQLite approval-decision write path updates
    the `TurnSnapshot` and appends its `ApprovalAuditEntry` under one
    `ConnectionManager.transaction()`, so the decision and audit record cannot
    diverge. `TurnStateStore` and `ApprovalAuditStore` remain the public domain
    interfaces; the workspace persistence module owns this internal transaction
    coordinator.

### Inbox and CLI

44. As a framework developer, I want `InboxMQ` to formalize topic lifecycle
    (`pending → active → idle → expired`) with `receive`, `consume`, `peek`,
    `count`, `clear`, `sessions_with_pending`, `deliver`, `wakeup`,
    `wait_wakeup`, `reap_expired`, so that the inbox is a proper MQ, not a
    file append log.

45. As a CLI user, I want `modexctl send` to open a short-lived SQLite
    connection to the target workspace's `state.db` and INSERT into
    `inbox_messages` within a `BEGIN IMMEDIATE` transaction, so that CLI
    delivery is atomic with the framework server's consumption.

46. As a CLI user, I want `MODEX_INBOX_ROOT` to still point to
    `<workspace>/.modex/inbox` (the CLI derives `state.db` from its parent),
    so that no new environment variables are needed.

47. As a framework developer, I want CLI and framework server to switch to
    DB in the same release (no dual-write window for inbox), so that there
    is never a state where one writer is on DB and the other on files.

### Turn State

48. As a framework developer, I want a partial unique index
    `CREATE UNIQUE INDEX ... ON turn_snapshots (agent_id, session_id) WHERE
    phase IN ('running', 'suspended')`, so that the one-active-turn
    invariant is enforced by the database, not by scan-then-write.

49. As a framework developer, I want `TurnStateStore` to store the full
    `TurnSnapshot` as a versioned JSON payload plus indexed columns
    (agent_id, session_id, turn_id, phase, reason, timestamps), so that
    queries use indexes and the full snapshot is one column.

50. As a framework developer, I want `ApprovalTransaction` to remain inside
    `payload_json` (not a separate table), so that the single approval path
    (`ToolNode → ApprovalTransaction → TurnSnapshot → ApprovalRenderer`) is
    preserved.

### Cleanup and Cascade

51. As a framework developer, I want archive entries to have a retention
    policy (`max_entries`, `max_age_days`) enforced via
    `DELETE FROM memory_archive_entries WHERE ...` + delete Markdown
    directories, so that archive does not grow unbounded.

52. As a framework developer, I want completed turn snapshots to be cleaned
    by a background job (`DELETE WHERE phase IN ('completed', 'cancelled',
    'error') AND created_at < ?`), so that old turn state does not
    accumulate.

53. As a framework developer, I want inbox dead-letter and delivered-IDs to
    have TTL cleanup, so that dead-letter queue and dedup tables do not grow
    unbounded.

54. As a framework developer, I want a `SessionArtifactCleaner` ABC (framework
    layer) that coordinates DB row deletion + file directory deletion for
    session cascade cleanup, so that `SessionGarbageCollector` (business
    layer) delegates to one framework seam.

55. As a framework developer, I want orphan artifact scanning (artifacts
    without an index record) to go through `SessionArtifactCleaner`, so that
    crash-recovery cleanup uses the same seam as explicit deletion.

### ContextForkBuilder Simplification

56. As a framework developer, I want `ContextForkBuilder` to no longer write
    fork XML files — it queries the parent session's `MessageStore` for the
    last N active messages, applies lossy compaction, and returns the XML
    string directly — so that no file cleanup registry is needed.

57. As a framework developer, I want `SessionGarbageCollector`'s artifact
    list to drop `fork_contexts` (from 10 to 9 artifacts), so that the
    collector does not scan for orphaned fork files.

### Terminal State Store Removal

58. As a framework developer, I want `JsonTerminalStateStore` and its
    `save_state()`/`load_state()` path in `BaseTerminalManager` deleted
    (dead code — production never passes `storage_dir`), so that the
    architecture guard test for the save/load seam is also removed.

### Persistence Lifecycle

59. As a bot operator, I want SQLite connections opened during workspace
    materialization and closed during workspace eviction with
    `PRAGMA wal_checkpoint(TRUNCATE)` before `close()`, so that WAL files
    are merged into the main DB on clean shutdown.

60. As a bot operator, I want the global registry DB connection opened at
    `BotService.initialize()` (before workspace materialization) and closed
    at `BotService.stop()` (after all workspaces evicted), so that
    cross-workspace routing is available throughout the bot lifecycle.

61. As a bot operator, I want WAL crash recovery to be automatic (SQLite
    replays WAL on next open), so that a killed process does not lose
    committed transactions.

62. As a framework developer, I want one `aiosqlite.Connection` per workspace
    (not a connection pool), so that connection management is simple. If
    profiling shows contention, a read-only pool can be added later.

    **Coordination clarification:** every adapter operation and multi-step
    transaction on that shared connection is coordinated by
    `ConnectionManager`, so statements from two adapters cannot interleave
    between `BEGIN` and `COMMIT`. No LLM, network, or file I/O occurs while the
    transaction lock is held.

63. As a framework developer, I want migrations to be plain SQL files shipped
    with the package, tracked by a `schema_migrations` table, run by a
    ~30-line `MigrationRunner` (no Alembic), so that schema evolution is
    transparent and dependency-free.

    **Atomicity clarification:** each migration's schema changes and its
    `schema_migrations` row are committed atomically. Migration files contain no
    `BEGIN`/`COMMIT`; the runner supplies the explicit transaction and rolls it
    back as a unit on failure. `executescript()` is not treated as an implicit
    transaction boundary.

64. As a framework developer, I want IOC factories to select file or SQLite
    implementations per store based on a typed `PersistenceBackend` enum in
    `PersistenceConfig.backend`,
    so that stores can migrate to DB one at a time.

### PostgreSQL Replaceability

65. As a framework developer, I want store ABCs to expose domain operations
    (`enqueue`, `claim`, `ack`, `save_turn`, `find_active`, `append_event`)
    — never SQL, ORM sessions, or SQLite pragmas — so that adapters are
    backend-swappable.

66. As a framework developer, I want SQLite and future PostgreSQL adapters
    to run the same conformance test suite, so that portability is verified
    by tests, not by assertion.

67. As a framework developer, I want backend-specific claim/lock
    implementations to be allowed to differ (SQLite single-writer vs
    PostgreSQL row-level locking), so that the ABC contract defines
    semantics, not mechanism.

### File Implementation Refactor (required for ABC convergence)

68. As a framework developer, I want `DefaultScopedStorage` (file implementation)
    to simultaneously implement `MessageStore`, `KVStore`, `CursorStore`, and
    `ArchiveStore` (one class, four interfaces), so that `MemoryStoreBundle`'s
    all fields point to the same instance — matching current `MemoryStorage`
    usage where one object serves all concerns.

69. As a framework developer, I want `DirArchiveStorage` to simultaneously
    implement `KVStore` (state.json), `ArchiveStore` (archive_id/channel
    management + log methods), and `CursorStore`, so that the archive layer
    works through the bundle without a separate `MemoryStorage` type.

70. As a framework developer, I want `MarkdownKnowledgeStorage` (renamed to
    `MarkdownCoreMemoryStorage` per ADR-0035) to implement
    `KVStore` (filename=key, content=value) + `CursorStore`, so that the
    core memory layer (formerly "knowledge") works through the bundle.

71. As a framework developer, I want `InMemoryStorage`,
    `InMemoryStoreRegistry`, and `InMemoryRegistryStore` DELETED — they are
    only used in tests, never in production, and their `scope_key` parameter
    mismatch constrains the ABC design — so that tests migrate to SQLite
    durable temporary backend fixtures and the framework no longer carries
    these test-only implementations. This deletion names only these three
    implementations and any now-private support code used solely by them; it
    does not delete `InMemoryInboxServer`, runtime in-memory fakes, or unrelated
    in-memory adapters.

72. As a framework developer, I want all tests that currently use
    `InMemoryStorage` / `InMemoryStoreRegistry` / `InMemoryRegistryStore`
    migrated first to the corresponding temporary file-backed interface fixture,
    so the prefactor does not depend on SQLite adapters implemented later. The
    conformance suite subsequently runs the same contracts against temporary
    file and SQLite backends.

73. As a framework developer, I want all 60 call sites in `memory/layers/`,
    `memory/lifecycle.py`, `memory/cleanup.py`, `memory/consolidation/`,
    and `memory/registry/` updated from `storage.xxx()` to
    `bundle.{messages|kv|cursors|archive}.xxx()`, so that the ABC split works
    with both file and DB bundles.

74. As a framework developer, I want `MessageStore.prune_messages()` in the
    file implementation to remove messages from `messages.jsonl` (current
    overwrite behavior) AND return the pruned message content list, so that
    archive/pruned/URB consumers receive content — matching the DB
    implementation's contract.

75. As a framework developer, I want `MessageStore.pin_message()` /
    `unpin_message()` in the file implementation to mark messages with
    `_pinned: true` in the message dict metadata, so that `prune_messages`
    skips pinned messages.

76. As a framework developer, I want `MessageStore.cleanup_expired()` in the
    file implementation to be a no-op (file implementation does immediate
    physical delete, no soft_deleted state), so that the TTL cleanup contract
    is satisfied without behavior change.

77. As a framework developer, I want `MemoryStoreRegistry.resolve()` to
    return `MemoryStoreBundle` (Pydantic model, `arbitrary_types_allowed=True`,
    frozen) instead of `MemoryStorage`, so that callers receive a typed bundle
    of single-responsibility interfaces.

78. As a framework developer, I want `ArchiveStore` to absorb all methods
    from the cancelled `LogStore` (`append_log`, `read_logs`, `save_logs`)
    plus existing archive extensions (`read_archive_state`,
    `write_archive_state`, `append_channel_log`, `read_channel_logs`,
    `save_channel_logs`, `prune_to_max`, `cleanup_empty_dirs`), so that
    archive layer callers access all archive/log operations through one
    interface. `ArchiveStore` therefore has 10 methods: three log methods plus
    seven archive/channel/retention methods.

79. As a framework developer, I want the `memory/AGENTS.md` and
    `memory/core/AGENTS.md` documentation updated to reflect the ABC split
    (`MemoryStorage` → 4 ABCs + `MemoryStoreBundle`) and deletion of
    `InMemoryStorage`, so that future developers are not misled by stale
    documentation.

### Backend Selection — Framework vs Bot

80. As a framework developer, I want the framework to support BOTH file and
    SQLite implementations behind the split ABCs, so that framework users
    can choose their persistence backend.

81. As a bot operator, I want `examples/bot_project/` to select SQLite as
    its sole persistence backend (no file fallback for structured state
    stores), so that the bot gets transactional integrity, atomic inbox
    delivery, and database-enforced invariants.

82. As a framework developer, I want IOC factories to select file or SQLite
    based on a typed `PersistenceBackend` enum (`FILE` or `SQLITE`) carried by
    `PersistenceConfig.backend`,
    so that backend selection is a configuration decision, not a code change.

83. As a framework developer, I want the conformance test suite to be
    parameterized over both `file` and `sqlite` backends, so that both
    implementations are validated against the same ABC contract.

## Implementation Decisions

### New dependency

- `aiosqlite>=0.20.0,<1` added to framework `pyproject.toml` dependencies.
  `aiosqlite` is a pure-Python async wrapper over stdlib `sqlite3` — no C
  compilation, no native deps, cross-platform (Windows/macOS/Linux).
  CLI (`modexctl`) uses stdlib `sqlite3` directly — no new CLI dependency.

- No SQLAlchemy, no Alembic, no sqlite-utils. Migrations are plain SQL files
  + a `schema_migrations` table + a `MigrationRunner` class.

### Cross-platform verification (Windows/macOS/Linux)

All SQLite features used are supported on all three platforms by Python 3.12's
bundled `sqlite3` (SQLite 3.51.0+):

- WAL journal mode — works on local filesystems on all platforms. (Network
  filesystems are documented by SQLite as not recommended for WAL; this is a
  user deployment choice, not a code issue.)
- STORED generated columns — SQLite 3.31+ (Jan 2020), satisfied.
- Partial unique indexes (`CREATE UNIQUE INDEX ... WHERE ...`) — SQLite 3.8+
  (2013), satisfied.
- `ON CONFLICT DO NOTHING` — SQLite 3.24+ (2018), satisfied.
- `json_extract()` — SQLite 3.38+ (2022), satisfied.

`aiosqlite` uses `sqlite3` from stdlib on all platforms — no separate native
SQLite build. On Windows, Python bundles SQLite; on macOS, the system Python
and Homebrew Python both bundle SQLite; on Linux, all mainstream distros'
Python packages bundle SQLite. No platform-specific install steps.

### Module structure

New framework module `modex_agent/persistence/`:
- `connection.py` — `ConnectionManager`: open/close, PRAGMAs, migration runner.
- `migration.py` — `MigrationRunner`: version-tracked SQL file runner.
- `workspace_db.py` — `WorkspacePersistenceManager`: per-workspace connection
  + lazy store adapter factory.
- `registry_db.py` — `RegistryPersistenceManager`: global registry connection.
- `adapters/` — SQLite implementations of each store ABC.
- `migrations/workspace/` and `migrations/registry/` — SQL migration files,
  packaged via hatch `force-include`.

`ConnectionManager` is constructed with a typed database kind (`WORKSPACE` or
`REGISTRY`) and chooses only that migration stream. It owns the async
transaction lock and is passed to adapters instead of exposing a raw connection
for independently managed transactions.

### File implementation refactor (required, not optional)

The ABC split is not a DB-only change — it breaks every call site that
currently holds a `MemoryStorage` reference. The file implementations must
be refactored in the same change so both backends work:

- `DefaultScopedStorage` → implements `MessageStore` + `KVStore` +
  `CursorStore` + `ArchiveStore` (one class, four interfaces; bundle fields
  all point to same instance).
- `DirArchiveStorage` → implements `KVStore` + `ArchiveStore` + `CursorStore`.
- `MarkdownKnowledgeStorage` (renamed to `MarkdownCoreMemoryStorage` per ADR-0035) → implements `KVStore` + `CursorStore`.
- `InMemoryStorage` / `InMemoryStoreRegistry` / `InMemoryRegistryStore` →
  DELETED after their replacement file fixtures are available (test-only,
  signature mismatch). SQLite coverage arrives through conformance fixtures.
- 60 call sites in 8 files updated: `storage.xxx()` →
  `bundle.{messages|kv|cursors|archive}.xxx()`.
- `MemoryStoreRegistry.resolve()` returns `MemoryStoreBundle` instead of
  `MemoryStorage`.
- `ArchiveStore` absorbs `LogStore` methods + archive extensions.
- New `MessageStore` methods (`prune_messages`, `pin_message`,
  `unpin_message`, `delete_message`, `cleanup_expired`) implemented in file
  backends with appropriate semantics (physical delete = no soft_deleted
  state; pin = message metadata flag).

### Backend selection — framework vs bot

The framework supports both file and SQLite backends behind the split ABCs.
The bot (`examples/bot_project/`) selects SQLite as its sole backend via
`PersistenceConfig.backend = PersistenceBackend.SQLITE` in its IOC config.
Framework users who prefer file-based storage can select
`PersistenceBackend.FILE`. Conformance tests are parameterized over both
backends.

Stores that cannot be DB-replaced (`PrunedStorage`, `MediaStore`,
`ToolOverflowStore`) remain file-only with their existing ABCs — they are
not forced into the DB split.

### Scope system refactor

- `RecordScope` (frozen Pydantic model) replaces `MemoryScope.get_scope_key()`
  output. Both file and DB stores use `RecordScope`; file stores derive path
  segments via `to_path_segment(*dimensions)`, DB stores store the JSON
  column directly.
- `CompositeScope` rewritten to use `RecordScope.merge()` (field-level merge)
  instead of colon-string-join. It serves both file and DB backends.
- `canonical_json()` utility in `modex_agent/utils/canonical_json.py` —
  recursive deterministic serializer.
- `Scope` ABC replaces `MemoryScope` ABC; `extract(context) -> RecordScope`
  replaces `get_scope_key(context) -> str`.
- `PeerPairScope` not implemented (removed from documentation).
- Config `scope: str` → `scope: list[str]` (single string auto-wrapped).

### ABC convergence

| Action | ABCs |
|---|---|
| Split | `MemoryStorage` → `MessageStore` + `KVStore` + `CursorStore` + `ArchiveStore` |
| Cancel | `LogStore` (absorbed by `ArchiveStore` DB table) |
| Merge | `DeliveredIdTracker` → `InboxMQ` internal |
| Deepen | `RegistryStore` → `WorkspaceRegistryStore` (absorbs `RecentWorkspaces`) |
| Extract | `PoolSessionStore` → `PoolRoutingStore` ABC |
| Extract | `ExternalSessionStore` → `ExternalSessionMapStore` ABC |
| Evolve | `InboxServer` → `InboxMQ` (add `deliver()` sync method) |
| New | `ApprovalAuditStore` (immutable audit log) |
| New | `SessionArtifactCleaner` (DB + file cascade delete) |
| Remove | `JsonTerminalStateStore` (dead code) |
| Simplify | `ContextForkBuilder` (no file writes, pure computation) |

### Schema (workspace DB)

Tables: `sessions`, `pool_routing`, `inbox_topics`, `inbox_messages`,
`inbox_delivered_ids`, `inbox_dead_letter`, `turn_snapshots`,
`approval_audit_log`, `todos`, `transcript_events` (optional),
`attachments`, `memory_session_messages` (with state machine),
`memory_kv`, `memory_cursors`, `memory_revisions`, `memory_archive_state`,
`memory_archive_entries`, `external_session_map`, `workspace_meta`,
`schema_migrations`.

Every table with a `scope` column uses STORED generated columns for
dimension extraction. See `SCHEMA-DESIGN.md` for full DDL.

### Schema (registry DB)

Tables: `workspaces`, `session_workspace_map`, `schema_migrations`.

### Session message state machine

```
normal ──pin()──► pinned
   │                │
   │ auto-prune     │
   ▼                │
normal ◄─unpin()───┘
   │
   │ prune / explicit delete
   ▼
soft_deleted ──TTL──► physical DELETE
```

`CHECK ((state = 'soft_deleted') = (deleted_at IS NOT NULL))` enforces
consistency. Partial indexes: active messages
(`WHERE state IN ('normal', 'pinned')`), TTL cleanup
(`WHERE state = 'soft_deleted'`).

### CLI inbox delivery

`modexctl send` opens `sqlite3.connect(state.db, timeout=5.0)`, sets
`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000`, runs `BEGIN IMMEDIATE`,
upserts topic + inserts message + updates count, commits, closes. The
`UNIQUE(session_id, message_id)` constraint provides idempotency via
`ON CONFLICT DO NOTHING`.

### Lifecycle integration

- `WorkspacePersistenceManager` held on `PoolWorkspaceResources` (business
  layer resource bundle). Opened during `factory.materialize()`, closed
  during `factory.evict()` after producers, pollers, pools, broker, terminals,
  and final store flushes have stopped.
- `RegistryPersistenceManager` held on `BotService`. Opened at
  `initialize()` before workspace materialization, closed at `stop()` after
  all workspaces evicted.
- `ConnectionManager.close()` runs `PRAGMA wal_checkpoint(TRUNCATE)` then
  `connection.close()`.

## Testing Decisions

### Conformance test seam

Each Store ABC has a conformance test suite parameterized over two backends:

```
@pytest.fixture(params=["file", "sqlite"])
async def store(request) -> SomeStoreABC: ...

async def test_receive_and_consume(store: InboxMQ):
    # Same assertions, both backends
```

Tests verify ABC external behavior only — never SQL, file paths, or
implementation details. This is the highest seam (directly on the ABC
interface) and requires no new seams.

Prior art: `tests/unit/memory/` already uses this pattern for file-backed
store tests; the SQLite fixture extends it.

### SQLite-specific tests (not conformance)

- **WAL multi-connection concurrency**: framework connection + CLI connection
  writing concurrently; verify both succeed without corruption.
- **Partial unique index enforcement**: insert two `running` turns for same
  `(agent_id, session_id)`; verify second is rejected.
- **Generated column correctness**: insert scope JSON; verify derived columns
  match.
- **Migration idempotency**: run `MigrationRunner.run_pending()` twice; verify
  no error.
- **Crash recovery**: write data, close without checkpoint, reopen; verify
  data intact.
- **Cross-platform**: run all SQLite tests on Windows, macOS, Linux CI.

### Modules tested

- `modex_agent/persistence/connection.py` — ConnectionManager
- `modex_agent/persistence/migration.py` — MigrationRunner
- `modex_agent/persistence/workspace_db.py` — WorkspacePersistenceManager
- `modex_agent/persistence/registry_db.py` — RegistryPersistenceManager
- `modex_agent/persistence/adapters/*.py` — each store adapter
- `modex_agent/core/scope.py` — RecordScope, Scope ABC, CompositeScope
- `modex_agent/utils/canonical_json.py` — canonical_json
- `modexctl/main.py` — CLI deliver path

## Out of Scope

- **Data migration from existing files**: No importer, no shadow-read, no
  dual-write. The bot starts fresh with SQLite; existing `.modex/` file
  data is not imported. (User decision.) Note: the file *implementations*
  themselves ARE in scope — they must be refactored to implement the new
  split ABCs so the framework supports both backends. What's out of scope
  is migrating *data* from old file formats to the new DB.
- **PostgreSQL implementation**: The ABCs and conformance tests are designed
  for PostgreSQL replaceability, but no PostgreSQL adapter is built in this
  spec.
- **Redis/NATS/RabbitMQ**: No external messaging middleware. SQLite inbox
  + existing `InboxPoller` is sufficient for single-host deployment.
- **Connection pooling**: One `aiosqlite.Connection` per workspace. Pooling is
  deferred until profiling proves contention.
- **TraceStore DB migration**: Marked optional; JSONL trace continues.
- **ExperienceMetaStore DB migration**: Marked optional; deferred.
- **TranscriptStore framework promotion**: Stays in business layer.
- **Vacuum automation**: Manual `VACUUM` if needed; no auto-vacuum.
- **Removing file implementations from the framework**: File implementations
  remain as a framework capability. Only `InMemoryStorage` (test-only) is
  deleted. The bot chooses SQLite; the framework supports both.

## Further Notes

- **ADR-0023** documents the 11 decisions (D1-D11) underlying this spec.
- **`docs/design/hybrid-persistence/sqlite-deployment-and-lifecycle.md`** has the full deployment
  design (dependency rationale, connection management, migration system, bot
  lifecycle integration, IOC factory wiring, operational notes).
- **`CONTEXT.md`** has 10 new domain terms (RecordScope, Canonical JSON,
  State DB, Registry DB, Generated Scope Column, Session Message State
  Machine, Approval Audit Log, Session Artifact Cleaner, InboxMQ,
  MemoryStoreBundle).
- **`docs/design/hybrid-persistence/SCHEMA-DESIGN.md`** is the complete DDL
  reference. The research synthesis is background only and cannot override the
  ADR, this PRD, or the calibrated schema reference.
- **Cross-platform**: `aiosqlite` is pure Python (no C extension); stdlib
  `sqlite3` is bundled with Python on all platforms. No platform-specific
  installation steps, no native compilation, no system SQLite dependency.
  Verified on Windows (the development platform): WAL, generated columns,
  partial unique indexes, `ON CONFLICT DO NOTHING`, `json_extract` all work.
- **SessionGarbageCollector artifacts**: drops from 10 to 9 (fork_contexts
  removed). The collector's orphan scanning logic is extended through
  `SessionArtifactCleaner` to cover DB-row orphans (sessions/inbox/turns
  with no file artifacts) in addition to existing file-orphan scanning.
