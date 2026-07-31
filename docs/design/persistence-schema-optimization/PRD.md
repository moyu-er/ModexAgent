# Persistence Schema Optimization (Phase 1)

**Status**: ready-for-agent
**ADRs**: ADR-0028, ADR-0029, ADR-0031, ADR-0030
**Spec date**: 2026-07-19

## Phasing

This spec is **Phase 1** of a two-phase timestamp unification effort. The
split exists to keep the persistence-layer refactor focused and to avoid
sprawling unit-conversion changes across unrelated runtime code paths.

| Phase | Scope | Status |
|-------|-------|--------|
| **Phase 1** (this spec) | DB columns (all 18 tables → `INTEGER` ms), file-backend JSON payloads (`StorageRevision.updated_at`, archive entry `created_at`, `InboxMessage.timestamp` serialization, `WorkspaceRecord` fields), SQLite adapter boundary conversion for `TurnSnapshot` (DB stores int ms; runtime dataclass stays `float` seconds; adapter multiplies/divides at its boundary) | ready-for-agent |
| **Phase 2** (separate future spec) | Runtime in-memory dataclasses (`TurnSnapshot`, `TurnStateBase`, `OperationState`, `ApprovalRequestState`, `ApprovalTransaction`, `TurnSummary`, `StateQueryScope.created_before`) migrate from `float` seconds to `int` ms; ~109 reference sites across ~43 files; SQLite adapter boundary conversion removed | not started |

**Phase 2 trigger conditions** (any one):
1. A new feature needs timestamp arithmetic on runtime dataclasses.
2. The third float-vs-int unit bug appears.
3. A dedicated cleanup sprint is scheduled.

**Why split:** Phase 1 touches ~35 files (persistence + file backend +
models + business layer). Phase 2 touches ~43 additional files (runtime,
pipeline, hooks, approval, tests) that are unrelated to persistence.
Bundling them would balloon the change set and risk introducing
unit-conversion bugs in code paths unrelated to the schema migration.
Phase 1 establishes the DB-column and file-backend-payload standard now;
Phase 2 propagates it to in-memory types in a focused spec with its own
test plan.

**Key constraint:** Phase 1 must not break Phase 2's path. The adapter
boundary conversion (`int(snapshot.created_at * 1000)` on write,
`row["created_at"] / 1000.0` on read) is isolated to
`SqliteTurnStateStore` and documented in ADR-0029 §6, so Phase 2 can
remove it mechanically without re-deriving the conversion logic.

## Problem Statement

The persistence layer of the ModexAgent framework has accumulated structural
debt across its 20 database tables (18 workspace/registry tables + 2 system
migration tables) and the parallel file backend:

1. **Timestamp chaos.** Three incompatible representations coexist — `TEXT`
   ISO strings on `workspaces`/`session_workspace_map`/`schema_migrations`,
   `REAL` epoch seconds on 14 workspace tables, `INTEGER` milliseconds on
   `bot_webui_transcript_events`. Two live bugs resulted: `turn_snapshots`
   stores `created_at` as int-ms but `updated_at` as float-seconds in the
   same row (`turn_state_store.py:103-104`); `workspaces` declares TEXT
   columns but the adapter writes int-ms values into them. File-backend JSON
   payloads mix `datetime` objects, ISO strings, and int-ms with no
   consistent rule.

2. **Scope column duplication.** Every scoped table carries two identical
   JSON columns — `scope` and `scope_key` — with a `CHECK (scope =
   scope_key)` constraint that can only ever pass. 16 tables pay double
   storage and double index maintenance for one logical value.

3. **Dead `pool` dimension everywhere.** 13 tables carry a
   `pool TEXT GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED`
   column plus an `idx_*_pool` partial index, but **zero `WHERE pool = ?`
   query** ever fires in any adapter. The framework's own
   `PoolRoutingStore` routes via a separate `pool_name` business column.
   `pool` is a bot-project concept, not a framework dimension — yet it is
   hard-coded into the framework's `RecordScope` model and every scoped
   table.

4. **Two dead tables.** `inbox_dead_letter` has a full 8-column schema and
   2 indexes but **zero INSERT in production code** — `reap_expired()`
   deletes directly. `workspace_meta` has **zero reads and zero writes in
   production** — only two test files reference it.

5. **`inbox_topics` ghost state machine.** The table carries a four-state
   machine (`pending → active → idle → expired`), `last_active`,
   `message_count`, `consumer_task`. All four are `UPDATE`d on every
   consume/clear but **never `SELECT`ed** — `sessions_with_pending()`
   queries `inbox_messages` directly. The table's only real consumer is
   `SELECT topic_id ... WHERE scope_key = ?` to fill an FK. It is an FK
   anchor dressed up as a state machine.

6. **`inbox_messages` over-columnization.** Eight columns
   (`source_name`, `source_kind`, `content`, `envelope_session_id`,
   `envelope_agent_session_id`, plus four scope-generated columns) are
   written on every insert but **never appear in any `WHERE` clause**.
   They are `SELECT`ed only to reconstruct `InboxMessage` — a role that
   `payload_json` (storing the full dict) fulfills equally well, as the
   file backend already demonstrates.

7. **Adapter field-extraction logic scattered.** Each SQLite adapter that
   splits a dict across columns plus a residual JSON column does so
   imperatively, with hard-coded field lists at five sites per adapter
   (INSERT column list, VALUES placeholders, SELECT column list,
   row-to-dict reconstruction, WHERE-clause extraction). Adding one
   extracted column means editing all five sites with no compile-time
   sync check.

8. **`RecordScope` cannot grow business dimensions.** The framework's
   frozen Pydantic model hard-codes 12 isolation dimensions including
   `pool`. A business that wants a new dimension must edit the framework
   model, release a new framework version, and update every table's
   generated-column list — coupling business evolution to framework
   releases.

From the user's perspective: the persistence layer is harder to reason
about than it should be, timestamp bugs silently corrupt data, dead
schema slows writes and confuses readers, and extending the framework
with business-specific isolation dimensions requires framework changes.

## Solution

A coordinated Phase 1 refactor of the persistence layer that:

- **Unifies all timestamps** to `INTEGER` epoch milliseconds across DB
  columns and file-backend JSON payloads, with SQL triggers auto-managing
  `updated_at` and a single `now_ms()` utility as the producer.
- **Merges `scope`/`scope_key`** into a single `scope_key` column on all
  16 scoped tables, eliminating the redundant pair.
- **Removes the `pool` dimension** from the framework: `RecordScope`
  becomes a base class without `pool`; the bot project subclasses it as
  `BotRecordScope` re-adding `pool`. All `pool` generated columns and
  `idx_*_pool` indexes are dropped.
- **Drops dead tables** `inbox_dead_letter` and `workspace_meta`.
- **Minimizes `inbox_topics`** to an FK anchor (4 columns + 2 timestamps),
  removing the ghost state machine.
- **Simplifies `inbox_messages`** by removing 11 unused columns and
  storing the full `InboxMessage` dict in `payload_json`.
- **Introduces `ColumnProjection`** — a declarative field-mapping
  abstraction that lets SQLite adapters split a dict into columns plus
  residual JSON without scattered imperative code.
- **Aligns file-backend JSON payloads** with SQLite column types so a
  `FILE`↔`SQLITE` backend switch produces semantically identical data.

Phase 2 (runtime dataclass `float`→`int` ms migration, 109 reference
sites across 43 files) is deferred to a separate spec.

## User Stories

1. As a framework developer, I want all timestamp columns to use the same
   `INTEGER` millisecond type, so that I never have to reason about
   whether a column is in seconds, milliseconds, or ISO strings.
2. As a framework developer, I want `updated_at` to be auto-managed by a
   SQL trigger, so that forgetting to set it on an `UPDATE` does not
   leave a stale value.
3. As a framework developer, I want `updated_at` triggers to skip when I
   explicitly set the value, so that manual timestamp control (e.g.,
   backdating a correction) still works.
4. As a framework developer, I want a single `now_ms()` utility as the
   only timestamp producer, so that there is one place to audit and one
   import to use.
5. As a framework developer, I want `scope` and `scope_key` merged into
   one column, so that I do not maintain a redundant pair enforced by a
   tautological CHECK constraint.
6. As a framework developer, I want `pool` removed from the framework's
   `RecordScope`, so that the framework stays pool-agnostic and the bot
   project owns its pool concept.
7. As a bot developer, I want to subclass `RecordScope` as
   `BotRecordScope` with a `pool` field, so that I can add business
   isolation dimensions without framework changes.
8. As a framework developer, I want a base `RecordScope` and a
   `BotRecordScope` subclass to produce different canonical JSON, so
   that framework-managed and business-scoped records naturally land in
   separate storage buckets by construction.
9. As a framework developer, I want dead tables (`inbox_dead_letter`,
   `workspace_meta`) dropped, so that the schema does not carry
   speculative structure with no consumer.
10. As a framework developer, I want `inbox_topics` reduced to an FK
    anchor, so that I do not waste time understanding a ghost state
    machine that no query reads.
11. As a framework developer, I want `inbox_messages` to store the full
    `InboxMessage` dict in `payload_json`, so that the SQLite backend
    matches the file backend's data shape and the two are
    interchangeable.
12. As a framework developer, I want a declarative `ColumnProjection`
    abstraction, so that adding an extracted column is a one-line change
    to a projection tuple instead of a five-site scatter.
13. As a framework developer, I want `ColumnProjection` to handle the
    `content` field's `str`-vs-`list[dict]` duality via a `ContentCodec`,
    so that multimodal edge cases round-trip correctly without inline
    `isinstance` branches.
14. As a framework developer, I want file-backend JSON payloads to use
    the same `int` ms timestamps as SQLite columns, so that switching
    persistence backends does not change data semantics.
15. As a framework developer, I want `ChatMessage.created_at` to stay an
    ISO string inside `message_json`, so that display-only business
    timestamps are not coupled to storage metadata types.
16. As a framework developer, I want `InboxMessage.timestamp` to stay
    `datetime` in the ABC contract, so that both backends convert at
    their boundary without changing the ABC signature.
17. As a framework developer, I want `TurnSnapshot.created_at` to remain
    `float` seconds in Phase 1 with the SQLite adapter converting at its
    boundary, so that the runtime dataclass migration (Phase 2) is
    decoupled from the schema migration.
18. As a framework developer, I want all 12 store ABCs' method signatures
    to remain unchanged, so that the refactor is transparent to ABC
    consumers.
19. As a framework developer, I want indexes to match real query paths,
    so that no dead index slows writes and no hot query lacks an index.
20. As a framework developer, I want the `SqliteSessionDatabaseCleaner`'s
    hardcoded table list to use `scope_key` uniformly, so that the
    cleaner works against the merged column without per-table
    special-casing.
21. As a framework developer, I want the `pool`-aware backward-compat
    fallback in `SqliteSessionDatabaseCleaner` deleted, so that dead
    code does not accumulate.
22. As a framework developer, I want `WorkspaceRecord.created_at` and
    `.last_active` to be `int` ms, so that the model matches the DB
    column type.
23. As a framework developer, I want `StorageRevision.updated_at` to be
    `int` ms, so that file-backend and SQLite-backend revisions compare
    as integers.
24. As a framework developer, I want archive entry `created_at` to be
    `int` ms in both backends, so that archive entry ordering is
    consistent across persistence modes.
25. As a framework developer, I want framework migration SQL to define
    the new schema directly (rewriting `001_initial.sql`), so that a
    fresh workspace gets the optimized schema without a chain of
    alter-migrations.
26. As a bot developer, I want any one-time data migration script (for
    existing workspaces) to live in the bot project, not the framework,
    so that the framework does not carry one-time migration code.
27. As a framework developer, I want `ColumnProjection` to have its own
    unit tests covering `split`/`assemble` round-trip and codec
    behavior, so that the new abstraction is validated in isolation.
28. As a framework developer, I want the conformance test suite to
    remain the primary behavioral seam, so that ABC contract
    preservation is verified across both backends.
29. As a framework developer, I want SQLite-specific schema assertions
    updated to the new schema, so that physical structure (columns,
    indexes, triggers, CHECK constraints) is verified.
30. As a framework developer, I want architecture guard tests to prevent
    re-introduction of dropped tables (`workspace_meta`,
    `inbox_dead_letter`), so that the schema does not regress.
31. As a framework developer, I want a Phase 2 TODO recorded for runtime
    dataclass `float`→`int` ms migration, so that the deferred work is
    not forgotten.
32. As a framework developer, I want the `ApprovalAuditStore` ABC
    relocation (out of `persistence/adapters/`) to remain out of scope
    for this spec, so that the refactor stays focused on schema and
    timestamp concerns.
33. As a framework developer, I want the
    `BotWorkspaceMigrationRunner` code-deduplication with the framework
    `MigrationRunner` to remain out of scope for this spec, so that the
    refactor stays focused.
34. As a framework developer, I want the
    `SqliteSessionDatabaseCleaner` hardcoded UNION refactor to a
    registry pattern to remain out of scope for this spec, so that the
    refactor stays focused (the UNION is updated to use `scope_key`
    uniformly, but not restructured).

## Implementation Decisions

### ADRs recorded

Four ADRs capture the load-bearing decisions; this spec implements them:

- **ADR-0028** — `RecordScope` base/subclass split and `pool` removal
- **ADR-0029** — Epoch millisecond timestamp unification (Phase 1 portion:
  DB columns + file-backend JSON payloads; Phase 2 deferred for runtime
  dataclasses)
- **ADR-0030** — `ColumnProjection` SQLite adapter field-extraction
  abstraction
- **ADR-0031** — Persistence schema simplification (`scope`/`scope_key`
  merge, dead-table removal, `inbox_topics` minimization,
  `inbox_messages` simplification)

### New framework modules

- **`modex_agent/utils/time.py`** — `now_ms() -> int` and `now_s() ->
  float`. The single timestamp producer. `core/session_id.py` re-exports
  `now_ms` for backward compatibility.
- **`modex_agent/persistence/column_projection.py`** —
  `ColumnProjection`, `ColumnField`, `ColumnCodec`, `IdentityCodec`,
  `ContentCodec`. Declarative dict↔(columns + residual JSON) mapping.
  `ContentCodec.encode(column, value)` returns a `dict[str, Any]` of
  column→value, allowing one field to fan out into `content` +
  `is_content_json`. `split(data)` removes extracted keys from the
  residual; `assemble(columns, json_str)` re-injects under the first
  candidate key only.

### Modified framework modules

**Scope & models:**
- `modex_agent/core/scope.py` — `RecordScope` loses the `pool` field;
  becomes the framework base class with 11 dimensions
  (`workspace_id`, `session_id`, `session_prefix`, `agent_id`,
  `agent_role`, `user_id`, `tenant_id`, `channel`, `chat_id`,
  `invocation_id`, `parent_session_id`).
- `modex_agent/workspace/record.py` — `WorkspaceRecord.created_at` and
  `.last_active` change from `str` (ISO-8601) to `int` (ms epoch).
- `modex_agent/workspace/registry.py` and `modex_agent/workspace/store.py`
  — `_now_iso()` helpers replaced with `now_ms()` calls.
- `modex_agent/memory/core/models.py` — `StorageRevision.updated_at`
  changes from `datetime` to `int` (ms epoch).
- `modex_agent/memory/archive_models.py` — `ArchiveEntry.created_at`
  changes to `int` (ms epoch) if currently `datetime`.
- `modex_agent/runtime/models.py` — **Phase 1: unchanged.**
  `TurnSnapshot.created_at`, `TurnStateBase`, `OperationState`,
  `ApprovalRequestState`, `ApprovalTransaction`, `TurnSummary`,
  `StateQueryScope` all keep `float` seconds. Phase 2 spec will migrate
  them to `int` ms.

**SQLite adapters (11 files in `modex_agent/persistence/adapters/`):**
All adapters: `scope` column references → `scope_key`; `time.time()` →
`now_ms()`; time column writes use `now_ms()` or rely on SQL `DEFAULT`.

- `message_store.py` — adopts `_MESSAGE_PROJECTION`
  (`message_id`, `role`, `content`+`is_content_json` via `ContentCodec`,
  `token_count`); removes the dead `role` column write-only pattern
  (role becomes a real indexed column with CHECK); `json_extract`-based
  message_id lookups become column equality; `memory_revisions` writes
  drop `scope` column.
- `inbox_mq.py` — adopts `_INBOX_PROJECTION` (`message_id`,
  `message_type`, `session_id`); removes 5 dead business columns
  (`source_name`, `source_kind`, `content`, `envelope_session_id`,
  `envelope_agent_session_id`) and 4 dead scope-generated columns
  (`pool`, `agent_id`, `session_prefix`, `parent_session_id`,
  `invocation_id`); `inbox_topics` INSERT drops to 3 columns
  (`owner_scope_key`, `scope_key`, `session_id`); 5
  `UPDATE inbox_topics SET state/last_active/message_count` calls
  deleted; `inbox_delivered_ids` INSERT drops `session_id`; FK
  simplifies to single-column `scope_key` → `inbox_topics(scope_key)`;
  `deliver()` sync path updated identically; `_row_to_message` uses
  `_INBOX_PROJECTION.assemble()`.
- `turn_state_store.py` — `scope` → `scope_key`; `now = time.time()` →
  `now = now_ms()`; `created_at` write converts
  `int(snapshot.created_at * 1000)` (float seconds → int ms, Phase 1
  boundary conversion); `_decode` re-injects
  `row["created_at"] / 1000.0` into the payload before codec decode;
  `list_active_turns` converts `int(scope.created_before * 1000)` for
  the SQL parameter.
- `session_store.py` — `scope` → `scope_key`; `_SESSION_COLUMNS`
  unchanged (still reads `agent_id`/`parent_session_id` from generated
  columns); timestamp writes use `now_ms()`.
- `todo_store.py` — `scope` → `scope_key`; `updated_at` write uses
  `now_ms()` or omitted (DEFAULT + trigger).
- `approval_audit_store.py` — `scope` → `scope_key`; `decided_at` uses
  `now_ms()`.
- `kv_store.py` — `scope` → `scope_key`; `updated_at` uses `now_ms()`.
- `cursor_store.py` — `scope` → `scope_key`; `updated_at` uses
  `now_ms()`.
- `archive_store.py` — `scope` → `scope_key` on `memory_archive_entries`
  and `memory_archive_state`; `created_at`/`updated_at` use `now_ms()`.
- `external_session_map_store.py` — `scope` → `scope_key`;
  `last_committed_at` uses `now_ms()`.
- `pool_routing_store.py` — `scope` → `scope_key`; `SELECT pool_name,
  pool` → `SELECT pool_name` (drop dead generated column); `created_at`/
  `updated_at` use `now_ms()` or DEFAULT.
- `workspace_registry_store.py` — `WorkspaceRecord` field types
  (`int` ms) flow through; `metadata_json` write handles
  `NOT NULL DEFAULT '{}'`.

**Cleaner:**
- `modex_agent/persistence/session_cleanup.py` —
  `_SCOPE_DISCOVERY_SQL` changes `SELECT scope FROM {table}` →
  `SELECT scope_key FROM {table}`; `_SCOPE_DELETES` changes
  `WHERE scope = ?` → `WHERE scope_key = ?`; the
  `if scope.pool is not None:` backward-compat fallback (line 105) is
  deleted; `_INBOX_CHILD_TABLES` removes `"inbox_dead_letter"`;
  `time.time()` → `now_ms()`.

**File backend (4 files in `modex_agent/memory/stores/`):**
- `scoped_file.py` — `self._updated_at = datetime.now(UTC)` →
  `self._updated_at = now_ms()` (3 sites); archive entry
  `"created_at": ... or datetime.now(UTC).isoformat()` → `... or
  now_ms()` (2 sites).
- `file.py` — `updated_at=datetime.now(UTC)` → `updated_at=now_ms()`;
  `now = time.time()` → `now = now_ms()`.
- `scoped_in_memory.py` — same as `scoped_file.py` (2 + 2 sites).
- `dir_archive.py` — `updated_at=datetime.now(UTC)` →
  `updated_at=now_ms()`; archive entry `created_at` → `now_ms()`.

**File backend consumers:**
- `modex_agent/memory/layers/archive.py` —
  `entry.created_at.isoformat()` calls (2 sites) removed; `created_at`
  is now `int` ms, used directly.
- `modex_agent/memory/default_system.py` —
  `e.created_at.isoformat()` call removed.

**Inbox file backend:**
- `modex_agent/multi_agent/inbox/server_local.py` —
  `message.timestamp.isoformat()` →
  `int(message.timestamp.timestamp() * 1000)` (2 sites);
  `datetime.fromisoformat(data["timestamp"])` →
  `datetime.fromtimestamp(data["timestamp"] / 1000, tz=UTC)` (2 sites).
  `InboxMessage.timestamp` stays `datetime` in the ABC contract.

### New business-layer modules

- **`examples/bot_project/bot/scope.py`** —
  `BotRecordScope(RecordScope)` with `pool: str | None = None`.

### Modified business-layer modules

8 sites change `RecordScope(pool=...)` → `BotRecordScope(pool=...)`:
- `bot/workspace/pool_data.py`
- `bot/service/external_strategy.py`
- `bot/service/_assembly_helpers.py`
- `bot/service/web_ui_service.py`
- `bot/service/builders.py`
- `bot/service/session_gc.py` (the 4 `RecordScope(...)` constructions
  *without* `pool` stay on the base class; the `job.scope.pool` reads
  work because `job.scope` is typed `BotRecordScope` in the GC job
  model)

### Migration SQL

- `modex_agent/persistence/migrations/workspace/001_initial.sql` —
  **rewritten** to define the new schema directly. 14 workspace tables
  (drops `inbox_dead_letter` and `workspace_meta`; merges `scope`/
  `scope_key`; removes `pool` generated columns + `idx_*_pool` indexes;
  minimizes `inbox_topics`; simplifies `inbox_messages`; adds
  `created_at` where missing; all timestamps `INTEGER` ms with
  `DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)`;
  `updated_at` triggers on all mutable tables; new indexes matching
  query paths). No incremental `002_*.sql` — the initial migration
  defines the target state.
- `modex_agent/persistence/migrations/registry/001_initial.sql` —
  **rewritten** for `workspaces` (ms timestamps, `is_home` CHECK,
  `metadata_json NOT NULL DEFAULT '{}'`) and `session_workspace_map`
  (adds `created_at`/`updated_at`).
- **No data-migration SQL in the framework.** Existing workspaces that
  need their old data converted will use a one-time script archived in
  `examples/bot_project/` (out of framework scope; not part of this
  spec's deliverables).

### Schema shape (per-table summary)

For each table, the target columns (excluding PK/FK boilerplate):

- `memory_session_messages` — `scope_key`, `seq`, `message_id`, `role` (CHECK),
  `content`, `is_content_json`, `token_count`, `message_json`
  (residual), `created_at`/`updated_at` (ms + trigger), `state` (CHECK).
  Indexes: active partial, ttl partial (on `updated_at`), state, message_id
  partial.
- `inbox_messages` — `topic_id`, `owner_scope_key`, `scope_key`,
  `session_id`, `message_id`, `message_type` (CHECK),
  `payload_json` (full dict), `state` (CHECK), `seq`,
  `created_at`/`updated_at` (ms + trigger), `consumed_at`. Indexes:
  scope+state+seq, owner+pending partial, owner+expired partial.
- `inbox_topics` — `topic_id`, `owner_scope_key`, `scope_key`,
  `session_id`, `created_at`/`updated_at` (ms + trigger). Index:
  owner. No state machine.
- `inbox_delivered_ids` — `scope_key`, `message_id`, `owner_scope_key`,
  `delivered_at` (ms). PK `(scope_key, message_id)`. FK
  `scope_key` → `inbox_topics(scope_key)`. Index: owner+delivered_at.
- `turn_snapshots` — `snapshot_pk`, `session_id`, `agent_id`, `turn_id`,
  `scope_key`, `agent_kind`, `phase` (CHECK), `reason`,
  `created_at`/`updated_at` (ms + trigger), `schema_version`,
  `payload_json`. Indexes: active-unique partial, session, phase
  partial, created.
- `sessions` — `session_pk`, `session_id`, `scope_key`,
  `session_prefix`/`agent_id`/`parent_session_id` (generated),
  `created_at`/`updated_at` (ms
  + trigger), `metadata_json`. Indexes: prefix, parent.
- `todos` — `session_id`, `scope_key`, `items_json`,
  `created_at`/`updated_at` (ms + trigger).
- `approval_audit_log` — `id`, `turn_uuid`, `session_id`, `scope_key`,
  `agent_id`, `turn_id`, `tool_name`, `tool_call_id`, `decision` (CHECK),
  `deny_reason`, `decided_at` (ms), `decided_by`. Append-only (no
  `updated_at`, no trigger). Indexes: session+decided_at, turn.
- `memory_kv` — `scope_key`, `key`, `value_json`,
  `created_at`/`updated_at` (ms + trigger). PK `(scope_key, key)`.
- `memory_cursors` — `scope_key`, `cursor_name`, `cursor_value`,
  `created_at`/`updated_at` (ms + trigger). PK
  `(scope_key, cursor_name)`.
- `memory_revisions` — `scope_key`, `message_count`, `version`,
  `created_at`/`updated_at` (ms + trigger). PK `scope_key`.
- `memory_archive_entries` — `id`, `scope_key`, `archive_id`, `channel`,
  `summary`, `created_at` (ms). Append-only. Indexes: scope+channel+
  archive_id, scope+archive_id.
- `memory_archive_state` — `scope_key`, `next_archive_id`, `state_json`,
  `created_at`/`updated_at` (ms + trigger). PK `scope_key`.
- `external_session_map` — `modex_session_id`, `scope_key`,
  `provider_session_id`, `provider_kind` (CHECK), `last_committed_at`
  (ms), `invalidated` (CHECK 0/1), `created_at`/`updated_at` (ms +
  trigger). PK `modex_session_id`.
- `pool_routing` — `session_prefix`, `scope_key`, `pool_name`,
  `created_at`/`updated_at` (ms + trigger). PK `session_prefix`. Index:
  pool_name.
- `workspaces` — `workspace_id`, `target_path` (UNIQUE), `display_name`,
  `created_at`/`last_active` (ms), `is_home` (CHECK 0/1),
  `metadata_json` (NOT NULL DEFAULT `'{}'`). PK `workspace_id`. Indexes:
  last_active, created_at.
- `session_workspace_map` — `session_prefix`, `workspace_id` (FK
  CASCADE), `created_at`/`updated_at` (ms + trigger). PK
  `session_prefix`. Index: workspace_id.
- `bot_webui_transcript_events` — **unchanged** (golden standard).
- `schema_migrations` / `bot_schema_migrations` — **unchanged** (system
  tables, `applied_at` stays TEXT).

### Trigger template

Every mutable table with `updated_at` gets:

```sql
CREATE TRIGGER trg_<table>_auto_updated_at
AFTER UPDATE ON <table>
FOR EACH ROW
WHEN NEW.updated_at IS OLD.updated_at
BEGIN
    UPDATE <table> SET updated_at = CAST(strftime('%s','now') AS INTEGER) * 1000
    WHERE rowid = NEW.rowid;
END;
```

Append-only tables (`approval_audit_log`, `memory_archive_entries`,
`bot_webui_transcript_events`, `inbox_delivered_ids`) get no trigger.

### ABC contract preservation

All 12 store ABCs' method signatures remain unchanged. The refactor is
internal to adapters and model classes. Specifically:
- `MessageStore.append_message(message: dict)` — unchanged
- `InboxMQ.receive(message: InboxMessage)` — unchanged
- `TurnStateStore.save_turn(snapshot: TurnSnapshot)` — unchanged
- `SessionStore.save(session: SessionInfo)` — unchanged
- `TodoStore.save(session_id, todos: list[TodoItem])` — unchanged
- `ApprovalAuditStore.append(entry)` — unchanged
- `KVStore`/`CursorStore`/`ArchiveStore` — unchanged
- `PoolRoutingStore`/`ExternalSessionMapStore`/`WorkspaceRegistryStore`
  — unchanged

### Phase 2 deferred (recorded as TODO)

The runtime dataclass `float`→`int` ms migration
(`TurnSnapshot`/`TurnStateBase`/`OperationState`/
`ApprovalRequestState`/`ApprovalTransaction`/`TurnSummary`/
`StateQueryScope`, 109 reference sites across 43 files) is **out of
scope** for this spec. Phase 1 uses adapter-boundary conversion
(`int(snapshot.created_at * 1000)` on write,
`row["created_at"] / 1000.0` on read). Phase 2 will be a separate spec
triggered by: any new feature needing timestamp arithmetic on runtime
dataclasses, the third float-vs-int unit bug, or a dedicated cleanup
sprint.

## Testing Decisions

### Primary seam: conformance suite (highest behavioral seam)

The conformance suite (`tests/conformance/`, 14 files) runs each store
ABC's contract against both FILE and SQLITE backends. It is the primary
verification that the refactor preserves ABC behavior. **Most of the
refactor's correctness is verified here.** Updates needed:

- `StorageRevision.updated_at` assertions: `datetime` → `int` ms
  comparisons.
- `WorkspaceRecord.created_at`/`.last_active` assertions: `str` → `int`
  ms.
- `TurnSnapshot.created_at` assertions: stay `float` (Phase 1); adapter
  conversion verified implicitly via round-trip.
- `InboxMessage.timestamp` assertions: stay `datetime`; serialization
  format change verified implicitly via round-trip.
- Any `pool`-related conformance assertions removed.

A good conformance test asserts **external behavior** (round-trip
equality, ordering, deduplication) not **internal storage shape** (which
columns exist, which are generated). The conformance suite already
follows this principle; we preserve it.

### Secondary seam: SQLite-specific schema assertions

`tests/conformance/test_sqlite_specific.py` verifies physical DB
structure. Updates:
- Smoke-test target: `workspace_meta` → `sessions` (any always-present
  table).
- `pool` generated-column checks: removed.
- Timestamp column type checks: `REAL`/`TEXT` → `INTEGER`.
- Trigger existence checks: added (one per mutable table).
- `scope`/`scope_key` dual-column checks: replaced with single
  `scope_key` checks.
- `inbox_dead_letter`/`workspace_meta` table existence checks: replaced
  with absence assertions (architecture guard).

### Tertiary seam: schema structure tests

`tests/unit/persistence/test_workspace_schema.py` and
`test_registry_schema.py` verify full DDL. Updates:
- All table definitions updated to target schema.
- `inbox_dead_letter`/`workspace_meta` test blocks removed.
- `pool` generated-column test blocks removed.
- New: `ColumnProjection`-driven column presence checks for
  `memory_session_messages` and `inbox_messages`.

### New seam: `ColumnProjection` unit tests

`tests/unit/persistence/test_column_projection.py` (new file) —
property-based round-trip tests:
- `split` then `assemble` returns a dict equal to the input (for all
  codec combinations).
- `ContentCodec` round-trips `str` and `list[dict]` via
  `is_content_json` flag.
- Candidate key priority: first hit wins on `split`; first key
  re-populated on `assemble`.
- Residual JSON excludes all extracted keys (all candidates, not just
  the hit).
- Empty dict and missing keys handled gracefully.

Prior art: `tests/unit/persistence/test_connection.py` and
`test_migration.py` follow the same isolated-unit-test pattern.

### Cleaner tests

`tests/unit/persistence/test_session_cleanup.py` — updates:
- `inbox_dead_letter` cascade test block removed.
- `scope` → `scope_key` in all SQL assertions.
- `pool` fallback test removed.
- Timestamp unit assertions: seconds → ms.

### Architecture guard tests

`tests/architecture/` — new assertions:
- No production code references `workspace_meta` or `inbox_dead_letter`
  (preventing regression).
- `RecordScope` does not have a `pool` field (preventing regression).
- `utils/time.py` exports `now_ms` (preventing relocation regression).

Prior art: `tests/architecture/test_dead_code_gone.py` and
`test_dependency_tree.py` follow the same pattern.

### What we do NOT test

- **Adapter internal storage shape** (e.g., "message_json does not
  contain `role`"). These are implementation details that may evolve.
  Verified indirectly via conformance round-trip.
- **Phase 2 runtime dataclass types**. Out of scope.
- **Data migration from old schema**. Out of scope (no data migration
  in framework).
- **PostgreSQL adapter**. Future work; this spec is SQLite + file only,
  but the schema design is PG-compatible (documented in ADRs).

## Out of Scope

1. **Phase 2 runtime dataclass migration** — see the **Phasing** section
   at the top of this spec. Phase 1 uses adapter-boundary conversion;
   Phase 2 (separate spec, triggered by the conditions listed above)
   will propagate `int` ms to `TurnSnapshot`/`TurnStateBase`/
   `OperationState`/`ApprovalRequestState`/`ApprovalTransaction`/
   `TurnSummary`/`StateQueryScope` and remove the conversion.
2. **`BotWorkspaceMigrationRunner` code deduplication** with framework
   `MigrationRunner` (extract `NamespacedMigrationRunner` base class).
   Separate spec; not blocking.
3. **`SqliteSessionDatabaseCleaner` registry-pattern refactor** (replace
   hardcoded 16-table UNION with a shared scoped-table registry). This
   spec updates the UNION to use `scope_key` uniformly but does not
   restructure it. Separate spec.
4. **`ApprovalAuditStore` ABC relocation** (out of
   `persistence/adapters/` into `runtime/` or `approval/`). Separate
   spec.
5. **`JsonFileApprovalAuditStore`** (file-backend implementation of
   `ApprovalAuditStore`). Currently no file backend; out of scope.
6. **PostgreSQL adapter**. Schema design is PG-compatible (documented
   in ADRs) but no PG adapter is implemented in this spec.
7. **Data migration from existing old-schema databases**. Framework
   migration SQL defines the new schema directly. One-time data
   migration scripts, if needed, live in the bot project as archived
   utilities, not in framework code.
8. **`ChatMessage.created_at` ISO-string unification**. It is
   display-only business data inside `message_json`, never parsed for
   storage decisions. Stays ISO-8601 string.
9. **`attachments` table implementation** (designed in SCHEMA-DESIGN.md
   but never built). Separate spec if needed.

## Further Notes

- **Per the user's directive, existing databases are not migrated.**
  Fresh workspaces get the new schema via the rewritten `001_initial.sql`.
  Existing workspaces that need their data preserved will use a one-time
  script archived in `examples/bot_project/` — this is a bot-project
  concern, not a framework concern, and is not part of this spec's
  deliverables.

- **The `SqliteSessionDatabaseCleaner._SCOPE_DISCOVERY_SQL` hardcoded
  UNION remains a known coupling** (P2 cleanup for a separate spec).
  This spec updates it to use `scope_key` uniformly but does not
  restructure it into a registry pattern.

- **The trigger edge case** (backend explicitly sets `updated_at` to a
  value identical to `OLD.updated_at` → trigger overwrites with current
  time) is documented in ADR-0029 and accepted. Setting `updated_at` to
  its previous value is an anti-pattern; the trigger's override is the
  correct semantic.

- **`InboxMessage.timestamp` stays `datetime` in the ABC contract.**
  Both backends convert at their boundary: file backend serializes as
  `int(timestamp.timestamp() * 1000)`, SQLite adapter stores in the
  `created_at` INTEGER ms column. This keeps the ABC signature stable
  while unifying the persisted representation.

- **`TurnSnapshot.created_at` Phase 1 boundary conversion** is
  adapter-internal. The ABC contract (`TurnStateStore.save_turn
  (snapshot: TurnSnapshot)`) stays `float` seconds until Phase 2.
  Phase 2 will remove the conversion and change the dataclass to `int`
  ms.

- **Phase 2 trigger conditions** (recorded for the future spec):
  (a) any new feature needing timestamp arithmetic on runtime
  dataclasses; (b) the third float-vs-int unit bug; (c) a dedicated
  cleanup sprint.

- **Implementation order suggestion** (from the self-check report):
  P0 infrastructure (`utils/time.py`, `column_projection.py`,
  `RecordScope` base split, migration SQL) → P0 adapters (11 files) →
  P0 cleaner → P0 models (`WorkspaceRecord`, `StorageRevision`,
  `ArchiveEntry`) → P0 file backend (4 files) → P0 file-backend
  consumers → P1 business layer (`BotRecordScope`, 8 sites) → P1 tests
  → P1 verification (conformance + bot e2e).

- **Self-check report** with full per-table schema, per-adapter change
  list, and risk assessment is at
  `C:\Users\GYT\AppData\Local\Temp\opencode\modexagent-self-check-report.md`
  (reference material for the implementing agent; not part of the spec
  deliverable).
