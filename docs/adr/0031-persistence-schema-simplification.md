# Persistence schema simplification — scope/scope_key merge, dead-table removal, inbox_topics minimization

## Status

Accepted (2026-07-19). Revises ADR-0023 D2 (scope representation) and the
`InboxMQ` glossary entry.

## Context

ADR-0023 D2 specified that every scoped workspace-DB table carry **two**
JSON columns: `scope` (the canonical JSON, source for generated columns)
and `scope_key` (the same canonical JSON, used as the uniqueness key),
with a `CHECK (scope = scope_key)` constraint enforcing their equality.
Sixteen tables carried this redundant pair.

Three other issues accumulated in the schema:

1. **`inbox_dead_letter`** — a fully-specified dead-letter table (8
   columns, 2 indexes, FK to `inbox_topics`) with **zero `INSERT` in
   production code**. `SqliteInboxMQ.reap_expired()` deletes expired
   messages directly from `inbox_messages`; it never moves them to a
   dead-letter table. The only `INSERT INTO inbox_dead_letter` statements
   are in two test files verifying cascade-delete behavior.

2. **`workspace_meta`** — a generic key-value table
   (`key TEXT PRIMARY KEY, value_json TEXT, updated_at REAL`) with **zero
   reads and zero writes in production code**. Only `test_workspace_schema.py`
   and `test_sqlite_specific.py` reference it (the latter as a
   connection-smoke-test target).

3. **`inbox_topics` over-engineering.** The table carried a four-state
   machine (`state IN ('pending','active','idle','expired')`),
   `last_active`, `message_count`, and `consumer_task`. Grep across all
   adapters and the business layer showed:
   - `state` is `UPDATE`d on every consume/clear but **never `SELECT`ed**
     — `sessions_with_pending()` queries `inbox_messages` directly,
     bypassing topics.
   - `message_count` is incremented/decremented but **never read**.
   - `last_active` is updated but **never used in `WHERE` or `ORDER BY`**.
   - `consumer_task` has **zero reads and zero writes**.
   
   The table's only real consumer is `SELECT topic_id FROM inbox_topics
   WHERE scope_key = ?` (to fill the `inbox_messages.topic_id` FK). It is
   an FK anchor, not a state machine.

4. **`inbox_messages` over-columnization.** Eight columns
   (`source_name`, `source_kind`, `content`, `envelope_session_id`,
   `envelope_agent_session_id`, plus four scope-generated columns
   `agent_id`, `session_prefix`, `parent_session_id`, `invocation_id`)
   were written on every insert but **never appeared in any `WHERE`
   clause**. They were `SELECT`ed only to reconstruct `InboxMessage` — a
   role that `payload_json` (storing the full dict) fulfills equally
   well, as the file backend already demonstrates.

## Decision

### 1. Merge `scope` and `scope_key` into a single `scope_key` column

All 16 workspace-DB tables drop the `scope` column. `scope_key` (the
canonical JSON of a `RecordScope` subclass — see ADR-0028) becomes the
sole scope representation:

- Generated columns derive from `json_extract(scope_key, '$.dim')`
  instead of `json_extract(scope, '$.dim')`.
- `UNIQUE` constraints and FKs reference `scope_key`.
- `CHECK (scope = scope_key)`, `CHECK (json_valid(scope))`, and the
  `CHECK (json_extract(scope, '$.session_id') = session_id)` invariant
  checks are removed. The `json_valid(scope_key)` check is retained where
  it aids debugging; the session_id invariant moves to
  application-layer validation (the adapter constructs `scope_key` from
  the same `RecordScope` that produced `session_id`, so they cannot
  diverge by construction).

The column name `scope_key` is retained (not renamed to `scope`) because
`scope_key` is the term used throughout adapter SQL, cleaner code, the
`SessionArtifactCleaner`, and the `RecordScope.canonical()` glossary
entry. Renaming would touch ~50 SQL sites for no semantic gain.

### 2. Drop `inbox_dead_letter`

`DROP TABLE inbox_dead_letter`. The dead-letter path was never
implemented; `reap_expired()` deletes directly. The
`SessionArtifactCleaner._INBOX_CHILD_TABLES` tuple in
`session_cleanup.py:40` removes the `"inbox_dead_letter"` entry — the
cleaner no longer attempts cascade-delete counting on a non-existent
table.

If a dead-letter requirement emerges in the future, it can be added back
as a new table with a clear use case, not as speculative schema.

### 3. Drop `workspace_meta`

`DROP TABLE workspace_meta`. The connection-smoke-test in
`test_sqlite_specific.py` switches to querying the `sessions` table (any
table that always exists). The `workspace_meta`-specific test in
`test_workspace_schema.py` is removed.

If workspace-level metadata is needed in the future, it should be
designed with a concrete consumer, not parked as a generic KV table.

### 4. Minimize `inbox_topics` to an FK anchor

```sql
CREATE TABLE inbox_topics (
    topic_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_scope_key TEXT    NOT NULL COLLATE BINARY,
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    session_id      TEXT    NOT NULL,
    created_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    updated_at      INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000),
    UNIQUE (scope_key),
    UNIQUE (topic_id, owner_scope_key, scope_key)
);
```

Removed: `scope`, `pool`, `state`, `last_active`, `message_count`,
`consumer_task`, and four redundant `UNIQUE` constraints
(`(owner_scope_key, scope_key)`, `(owner_scope_key, scope_key,
session_id)` — both implied by `UNIQUE(scope_key)`).

The adapter's `UPDATE inbox_topics SET state=..., last_active=...,
message_count=...` calls (5 sites in `inbox_mq.py`) are **deleted**. A
topic row is created on first message insert and deleted by the cleaner;
it never changes in between. The `updated_at` trigger is retained for
forward compatibility but, in practice, `inbox_topics` rows are
insert-once-delete-on-cleanup.

### 5. Simplify `inbox_messages` — remove 8 unused columns, store full dict in `payload_json`

Removed columns: `scope`, `pool`, `agent_id`, `session_prefix`,
`parent_session_id`, `invocation_id` (4 generated), `source_name`,
`source_kind`, `content`, `envelope_session_id`,
`envelope_agent_session_id` (5 business).

Retained columns: `topic_id`, `owner_scope_key`, `scope_key`,
`session_id`, `message_id`, `message_type`, `state`, `seq`, `created_at`,
`updated_at`, `consumed_at`, `payload_json`.

`payload_json` changes semantics from "stores `InboxMessage.metadata`
only" to "stores the **full** `InboxMessage` dict (source, content,
message_type, message_id, timestamp, metadata)". This matches the file
backend's `pending.jsonl` format (which always stored the full dict) —
the two backends now have the same data shape, differing only in
physical representation.

The 5 removed business columns were reconstructed from `payload_json` on
read anyway (`_row_to_message` in `inbox_mq.py:473-488` read
`source_name`/`content` from columns and `metadata` from `payload_json`,
then assembled an `InboxMessage`). After this change, `_row_to_message`
reads everything from `payload_json` via `ColumnProjection.assemble()` —
simpler, fewer columns to keep in sync.

### 6. Simplify `inbox_delivered_ids` — drop `session_id`, simplify FK

```sql
CREATE TABLE inbox_delivered_ids (
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    message_id      TEXT    NOT NULL,
    owner_scope_key TEXT    NOT NULL COLLATE BINARY,
    delivered_at    INTEGER NOT NULL,
    PRIMARY KEY (scope_key, message_id),
    FOREIGN KEY (scope_key) REFERENCES inbox_topics(scope_key) ON DELETE CASCADE
);
```

`session_id` was carried only for the old composite FK
`(owner_scope_key, scope_key, session_id)` to `inbox_topics`. With
`inbox_topics` minimized, the FK simplifies to single-column
`scope_key` → `inbox_topics(scope_key)` (which is `UNIQUE`). `session_id`
was never in any `WHERE` on this table. `owner_scope_key` is retained
because `reap_expired` filters by it.

### 7. Indexes: remove dead, add missing

Across the schema, indexes that had no matching `WHERE`/`ORDER BY` path
are removed (e.g. all `idx_*_pool`, `idx_messages_topic_state_seq`,
`idx_topics_state`, `idx_topics_session`, `idx_turn_parent`,
`idx_sessions_pool_*`). Indexes that match real query paths but were
missing are added (e.g. `idx_messages_scope_state_seq` for the
peek/consume hot path, `idx_messages_reap` for `reap_expired`,
`idx_routing_pool_name` replacing `idx_routing_pool`). The full per-table
index list is in the schema design document.

## Consequences

- **16 tables lose a redundant column.** `scope` is gone; `scope_key` is
  the sole scope representation. `CHECK (scope = scope_key)` constraints
  (which could only ever pass) are gone. Adapters, the cleaner, and tests
  update their `WHERE scope = ?` → `WHERE scope_key = ?` (one global
  find-replace, mechanically verifiable).

- **Two dead tables gone.** `inbox_dead_letter` and `workspace_meta` no
  longer occupy schema space or test maintenance. The cleaner's
  `_INBOX_CHILD_TABLES` tuple shrinks.

- **`inbox_topics` is honest about its role.** It is an FK anchor, not a
  state machine. Five `UPDATE` calls in `inbox_mq.py` that mutated
  never-read columns are deleted. Future developers won't waste time
  understanding a ghost state machine.

- **`inbox_messages` aligns with file backend.** Both backends now store
  the full `InboxMessage` dict; the SQLite backend extracts
  `message_id`/`message_type`/`session_id` to columns (for `WHERE`/
  `UNIQUE`) via `ColumnProjection` (ADR-0030) and keeps the rest in
  `payload_json`. The file backend stores the dict wholesale. ABC
  contract is identical.

- **`inbox_delivered_ids` FK simplifies.** Single-column `scope_key` FK
  instead of three-column composite. Easier to reason about, no
  `session_id` to keep in sync.

- **Index set shrinks and sharpens.** Every remaining index has a
  documented `WHERE`/`ORDER BY` path in the adapter code. Dead indexes
  no longer slow down writes.

- **`SessionArtifactCleaner._SCOPE_DISCOVERY_SQL`** (the 16-table
  `UNION`) is updated: `SELECT scope FROM {table}` →
  `SELECT scope_key FROM {table}`. The cleaner's hardcoded table list
  remains a known coupling (P2 cleanup: register scoped tables in a
  shared tuple that the cleaner and the migration generator both read),
  but the per-table SQL is now uniform.

- **Bot project `bot_webui_transcript_events` is untouched.** It was
  already the golden standard (ms-int `timestamp_ms`, proper indexes,
  `payload_json` for the full event dict). This ADR brings the other
  tables closer to its shape.

- **No data migration.** Per the user's directive, existing DBs are
  rebuilt from scratch; the migration SQL defines the new schema
  directly. A separate future ADR would handle online migration if
  production data ever needs to be preserved.
