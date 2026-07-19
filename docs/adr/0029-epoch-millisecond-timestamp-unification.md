# Epoch millisecond timestamp unification with SQL trigger auto-management

## Status

Accepted (2026-07-19). Revises the timestamp conventions implicit in ADR-0023.

## Context

The persistence layer grew three incompatible timestamp representations
across its 18 tables:

| Type | Tables | Semantics |
|------|--------|-----------|
| `TEXT DEFAULT (datetime('now'))` | `workspaces`, `session_workspace_map`, `schema_migrations` | ISO seconds string |
| `REAL NOT NULL` (epoch seconds, fractional) | 14 workspace-DB tables | Float seconds |
| `INTEGER NOT NULL` (epoch milliseconds) | `bot_webui_transcript_events` (`timestamp_ms`) | Int milliseconds |

Two live bugs resulted from this inconsistency:

1. **`turn_snapshots` unit mismatch** (`turn_state_store.py:103-104`):
   `created_at` receives `snapshot.created_at` (Python **int ms**), while
   `updated_at` receives `now = time.time()` (Python **float seconds**). The
   same row stores two timestamps in two different units. `idx_turn_created`
   and `list_active_turns`'s `created_at < ?` filter compare against
   caller-supplied values that may be in either unit.

2. **`workspaces` type mismatch** (`workspace_registry_store.py:122-123`):
   the adapter passes Python `int` (ms, from `WorkspaceRecord`) into a `TEXT`
   column. SQLite's dynamic typing tolerates this, but the stored value is
   not the ISO string the schema declares, so `ORDER BY last_active DESC`
   sorts numerically rather than lexicographically — correct only because
   all values happen to be integers.

Beyond the bugs, two structural gaps:

- **7 tables lack `created_at`** (`memory_kv`, `memory_cursors`,
  `memory_revisions`, `memory_archive_state`, `external_session_map`,
  `todos`, `workspace_meta`). 11 tables lack `updated_at`.
- **No SQL-level auto-management.** Every adapter manually computes
  `time.time()` at each write site (24 sites in `persistence/adapters/`).
  Forgetting to set `updated_at` on an `UPDATE` silently leaves the stale
  value. The framework has no safety net.

The user's requirement: timestamps should be **SQL-managed by default**,
but when the backend explicitly provides a value (manual insert/update with
a specific timestamp), SQL must **not override** it.

## Decision

### 1. Single canonical type: `INTEGER` epoch milliseconds

Every timestamp column on every table (with the single exception of
`schema_migrations.applied_at` and `bot_schema_migrations.applied_at` —
system tables, human-readable, never queried) becomes:

```sql
INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER) * 1000)
```

`strftime('%s','now')` is supported on every SQLite version; `unixepoch()`
is cleaner but requires SQLite 3.38+. PostgreSQL equivalent:
`BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM now()) * 1000)::BIGINT`.

### 2. `now_ms()` utility — the single Python-side producer

`core/session_id.py:24` already defined `now_ms()` for internal use. It is
**promoted** to `modex_agent/utils/time.py` as the framework's single
timestamp producer:

```python
# src/modex_agent/utils/time.py
import time

def now_ms() -> int:
    """Current Unix time in milliseconds (UTC)."""
    return int(time.time() * 1000)

def now_s() -> float:
    """Current Unix time in seconds (UTC, float). Kept for non-persistence
    callers that genuinely need second precision."""
    return time.time()
```

`core/session_id.py` re-exports `now_ms` for backward compatibility. All 24
`time.time()` sites in `persistence/adapters/` switch to `now_ms()`. No
adapter computes timestamps inline anymore.

### 3. SQL auto-managed `updated_at` via trigger

Every table with an `updated_at` column gets a trigger:

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

**Semantics**:

- Backend omits `updated_at` from `UPDATE SET ...` → `NEW.updated_at` equals
  `OLD.updated_at` (SQLite copies the old value forward) → trigger fires →
  auto-updates to current time. ✅
- Backend explicitly `SET updated_at = ?` with a different value →
  `NEW.updated_at IS NOT OLD.updated_at` → trigger skips → backend value
  wins. ✅
- **Edge case (accepted)**: backend explicitly `SET updated_at = <same value
  as OLD>` → trigger misidentifies this as "unchanged" and overwrites with
  current time. Setting `updated_at` to its previous value is an anti-pattern
  (it lies about modification time); the trigger overriding it is the
  correct behavior. Documented and accepted.
- `recursive_triggers` is `OFF` by default in SQLite, so the trigger's
  internal `UPDATE` does not re-fire itself. PostgreSQL uses
  `BEFORE UPDATE` + `IS NOT DISTINCT FROM` and mutates `NEW.updated_at`
  directly (no nested `UPDATE`), so the recursion concern does not apply.

All triggers use `WHERE rowid = NEW.rowid` — uniform across tables regardless
of PK shape (single-column, composite, `AUTOINCREMENT`, or `TEXT` PK). No
table in the schema is `WITHOUT ROWID`.

### 4. `created_at` — DEFAULT only, no trigger

`created_at` is set once at `INSERT` and never changed. SQL `DEFAULT`
satisfies the "auto-set unless explicitly provided" semantics by standard
SQL behavior: omit the column → `DEFAULT` fills it; provide a value → the
value is used; provide `NULL` explicitly → `NULL` is stored (callers must
not do this).

No `UPDATE`-protection trigger on `created_at`. Adapters never `SET
created_at` in `UPDATE` (grep-verified), and a trigger would block
legitimate future data-correction migrations. "`created_at` is immutable"
is an application-layer convention, not a DB constraint.

### 5. Append-only tables keep their domain timestamp only

`approval_audit_log.decided_at`, `memory_archive_entries.created_at`,
`bot_webui_transcript_events.timestamp_ms`, `inbox_delivered_ids.delivered_at`
are the single timestamp on their respective append-only tables. They do
**not** get an additional `created_at`/`updated_at` pair — the domain
timestamp already means "when this row came into being", and append-only
tables have no `UPDATE` path for `updated_at` to matter.

### 6. File backend timestamps — phased unification

The file backend (`DefaultScopedStorage`, `LocalFileInboxMQ`,
`DirArchiveStorage`, `JsonFileTurnStateStore`, `LocalFileSessionStore`)
stores timestamps **inside JSON payloads** in three mixed representations:

- `StorageRevision.updated_at: datetime` — serialized to ISO-8601 string
  by `json.dumps` (`memory/stores/file.py:85`, `scoped_file.py:69,90`,
  `scoped_in_memory.py:35,55`, `dir_archive.py:253`)
- Archive entry `created_at` — explicit ISO-8601 string
  (`scoped_file.py:314,331`, `dir_archive.py:290`,
  `scoped_in_memory.py:209,251`)
- `InboxMessage.timestamp` — `datetime.isoformat()` in `pending.jsonl`
  (`server_local.py:115,309`)
- `SessionInfo.created_at`/`updated_at` — already `int` ms (consistent
  with this ADR)
- `ChatMessage.created_at` — ISO-8601 string in `to_dict()`
  (`core/message.py:134`)

These split into two categories:

**Category A — Storage-metadata timestamps** (mirror DB columns, should
unify): `StorageRevision.updated_at`, archive entry `created_at`,
`InboxMessage.timestamp` (which the SQLite adapter maps to the
`created_at` column). These are **persisted-and-read-back** timestamps
that flow through an ABC contract (`StorageRevision`, `InboxMessage`,
archive entry dicts).

**Category B — Business-display timestamps** (one-way serialization,
not read back as timestamps): `ChatMessage.created_at` is formatted to
ISO string for human readability inside `message_json` and never parsed
back as a timestamp — `ChatMessage.from_dict`'s `_parse_created_at`
validator accepts the ISO string but the value is display-only. This
category is **out of scope** for unification.

**Phase 1 (this ADR, immediate):** Unify **Category A** file-backend
timestamps to `int` ms epoch, matching the DB columns they mirror.

- `StorageRevision.updated_at: datetime` → `int` (ms). The field type
  changes; `file.py:85`, `scoped_file.py:69,90`,
  `scoped_in_memory.py:35,55`, `dir_archive.py:253` switch from
  `datetime.now(UTC)` to `now_ms()`. The file-backend JSON format
  changes from `"2026-07-19T10:30:00+00:00"` to `1721380200000`.
- Archive entry `created_at` → `int` ms. `scoped_file.py:314,331`,
  `dir_archive.py:290`, `scoped_in_memory.py:209,251` switch from
  `datetime.now(UTC).isoformat()` to `now_ms()`.
- `InboxMessage.timestamp: datetime` **stays `datetime`** (it is the
  ABC contract type used by both backends), but the file backend
  serializes it as `int(message.timestamp.timestamp() * 1000)` instead
  of `message.timestamp.isoformat()`, and deserializes via
  `datetime.fromtimestamp(v / 1000, tz=UTC)`. The SQLite adapter does
  the same conversion to/from the `created_at` INTEGER ms column. Both
  backends now serialize the timestamp identically (int ms in JSON/DB).

**Phase 2 (separate spec, deferred):** Unify the **runtime in-memory
dataclasses** in `runtime/models.py` —
`TurnSnapshot.created_at`/`updated_at`,
`TurnStateBase.created_at`/`updated_at`,
`OperationState.created_at`/`updated_at`,
`ApprovalRequestState.created_at`,
`ApprovalTransaction.created_at`/`updated_at`,
`TurnSummary.completed_at`, and `StateQueryScope.created_before` — from
`float` (seconds) to `int` (ms). These are **not** directly persisted as
columns; they are serialized into `payload_json` via
`RuntimeStateCodec`. The SQLite adapter extracts `TurnSnapshot.created_at`
into the `turn_snapshots.created_at` column.

**Phase 1 adapter-side strategy for `TurnSnapshot`:** Because Phase 2
is deferred, the SQLite adapter performs the `float` ↔ `int` conversion
at the adapter boundary:
- On `save_turn`: `created_at = int(snapshot.created_at * 1000)` (float
  seconds → int ms)
- On `load_turn`: `snapshot_payload["created_at"] = row["created_at"] /
  1000.0` (int ms → float seconds) before `codec.decode_turn`
- On `list_active_turns` with `scope.created_before`: the adapter
  converts `int(scope.created_before * 1000)` before passing to the SQL
  `WHERE created_at < ?`

This conversion is **adapter-internal**; the ABC contract
(`TurnStateStore.save_turn(snapshot: TurnSnapshot)`) stays `float` until
Phase 2. The conversion cost (one multiply/divide per turn) is
negligible.

**Rationale for phasing:** Phase 2 touches 109 `.created_at`/
`.updated_at`/`.completed_at`/`created_before` reference sites across 43
files, including 6 dataclass definitions and their consumers in
runtime, persistence, pipeline, hooks, and tests. Doing it inline with
the schema migration would balloon the change set and risk introducing
unit-conversion bugs in code paths that are unrelated to persistence.
Phase 1 establishes the DB-column and file-backend-payload standard
now; Phase 2 propagates it to in-memory types in a focused spec with
its own test plan.

**Phase 2 is tracked as a TODO** in the spec that follows this ADR.
The trigger for Phase 2 is: any new feature that needs timestamp
arithmetic on runtime dataclasses, or the third time someone introduces
a `float`-vs-`int` unit bug.

## Consequences

- **One timestamp type, one producer, one trigger template.** No more
  `TEXT`/`REAL`/`INTEGER` mix; no more `time.time()` inline; no more
  forgotten `updated_at`.
- **`turn_snapshots` and `workspaces` bugs fixed.** Both were caused by the
  type mismatch this ADR eliminates.
- **PostgreSQL path is cleaner.** `BIGINT` ms epoch + `BEFORE UPDATE`
  trigger translates directly; no SQLite-specific `datetime('now')` in the
  schema.
- **`schema_migrations.applied_at` stays `TEXT`.** System table,
  human-readable, never compared. Documented as the sole exception.
- **File backend Phase 1 unified.** `StorageRevision.updated_at`,
  archive entry `created_at`, and `InboxMessage.timestamp`'s JSON
  serialization all switch to `int` ms. The file backend and SQLite
  backend now serialize the same data the same way, making a
  `FILE`↔`SQLITE` backend switch produce semantically identical payloads.
- **Runtime dataclasses (`TurnSnapshot` et al.) stay `float` in Phase 1.**
  The SQLite adapter performs `float`↔`int` conversion at its boundary.
  This is a known transitional state; Phase 2 (separate spec) will
  propagate `int` ms to `runtime/models.py` and remove the adapter
  conversion. The adapter conversion is a 1-line multiply/divide — not a
  performance concern.
- **`ChatMessage.created_at` stays ISO string.** It is display-only
  business data inside `message_json`, never parsed back as a timestamp
  for storage decisions. Unifying it would touch every `to_dict`/
  `from_dict` consumer for no storage benefit. Out of scope.
- **Supplement (2026-07-19): `created_at` column projection for fidelity.**
  ADR-0029 §6 originally declared `ChatMessage.created_at` out of scope
  because it was "never parsed back as a timestamp for storage decisions".
  Subsequent investigation found a **pre-existing fidelity bug**:
  `SqliteMessageStore._assemble_message` did not project the DB
  `created_at` column back into the message dict, so
  `ChatMessage.from_dict` fell back to the `default_factory` (current
  time). This caused downstream consumers (e.g. pruned-content
  time-range extraction in `PrunedManager._extract_time_range`) to see
  the cleanup time instead of the original message-creation time.
  The fix projects the `created_at` column (int ms) back into the dict
  and teaches `_parse_created_at` to distinguish int ms (>= 1e12) from
  int seconds via a threshold. `ChatMessage.to_dict()` **still emits
  ISO string** — the int-ms value is an internal round-trip format
  between the SQLite adapter and `ChatMessage.from_dict`, invisible to
  business code. This stays within the ADR-0029 §6 spirit (display
  format unchanged) while fixing the fidelity bug.
- **`now_ms()` lives in `utils/time.py`**, not `core/session_id.py`.
  `core/session_id.py` re-exports it for backward compatibility, but new
  imports should come from `utils/time`.
- **Edge case (trigger override of identical `updated_at`)** is documented
  and accepted. No workaround needed — the trigger's behavior is the
  correct semantic.
- **Phase 2 TODO recorded.** The spec following this ADR will list
  Phase 2 (runtime dataclass `float`→`int` ms) as a deferred work item
  with its own acceptance criteria.
