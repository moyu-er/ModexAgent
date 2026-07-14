# Hybrid persistence: per-workspace SQLite + file layer

Status: proposed (2026-07-14)

## Context

ModexAgent's `.modex/` directory currently stores all persistent state as local
files: JSON, JSONL, Markdown, and raw bytes. This works for single-process
operation but has structural problems in several stores:

1. **Inbox atomicity gap** — `LocalFileInboxServer` uses a process-local
   `asyncio.Lock`; `modexctl` uses an unrelated `FileLock` and appends directly
   to `pending.jsonl`. The two writers do not share a transaction boundary.
   `consume()` rewrites pending content before persisting delivered IDs — a
   crash between those operations can remove a message without recording
   delivery. The documented exactly-once guarantee is stronger than the
   implementation.

2. **Turn snapshot race** — `JsonFileTurnStateStore` performs a scan-then-write
   active-turn check without a lock. Two writers can pass the check; a crash
   can leave a partial snapshot.

3. **Session index scans** — `LocalFileSessionStore` recursively searches for
   a sanitized filename. Duplicate names under pool directories are ambiguous;
   parent/child queries require full directory scans.

4. **Scope key instability** — `CompositeScope` joins dimensions with `":"`
   into a flat path segment. This is irreversible (values containing `:` are
   ambiguous), supports only prefix matching, and hardcodes combinations at
   compile time.

5. **MemoryStorage god interface** — one ABC carries message CRUD, KV, logs,
   cursors, and archive extensions. A DB implementation would have to
   implement all five concerns in one class.

A naive "replace everything with SQLite" is wrong: Markdown knowledge files
(`SOUL.md`, `USER.md`, `MEMORY.md`), `EXPERIENCE.md` trees, media bytes, tool
overflow chunks, pruned JSONL (agent file-tool access), and config YAML are
correctly file-based. Their semantics require human editability, directory
structure, streaming, or path-based discovery that a database cannot replace.

## Decision

Adopt a **hybrid persistence architecture**:

### D1 — One SQLite database per workspace

```
<home>/.modex/_registry/state.db        Global registry (workspaces, session→workspace map)
<workspace>/.modex/state.db             Per-workspace state
<workspace>/.modex/memory/...           Markdown knowledge, archive documents (files)
<workspace>/.modex/media/...            Attachment bytes (files)
<workspace>/.modex/overflow/...         Tool overflow chunks (files)
<workspace>/.modex/pruned/...           Pruned JSONL for agent file-tool access (files)
<workspace>/.modex/runtime_state/...    OUTPUT.md, trace JSONL, command artifacts (files)
<workspace>/.modex/experiences/...      EXPERIENCE.md trees (files)
```

**Not one DB per pool** — cross-pool session trees and peer messaging need
cross-pool queries; per-pool DBs would multiply file count and require ATTACH.

**Not one global DB** — workspace is the natural deletion/backup/portability
unit; a global DB would require row-level filtering for workspace deletion and
force all workspaces to share one SQLite writer lock.

Workspace-level DBs allow different workspaces to write concurrently, preserve
workspace portability (copy `.modex/`), and keep the global registry DB small
(cross-workspace routing only).

### D2 — STORED generated columns for scope indexing

The `scope` column is a JSON object carrying all dimensional fields
(`pool`, `agent_id`, `session_id`, `session_prefix`, `user_id`, `tenant_id`,
etc.). `RecordScope` field names are canonical across Python and SQL; generated
columns extract those exact names.
Application code writes `scope` as the sole source for generated dimensions;
it still writes ordinary domain keys and payload columns. Generated columns
derive the indexed dimensions automatically:

```sql
pool TEXT GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
agent_id TEXT GENERATED ALWAYS AS (json_extract(scope, '$.agent_id')) STORED,
```

Because generated columns are real columns, they support **true B-tree
composite indexes** on any combination of dimensions — not function indexes
that degrade to scan+filter on multi-dimension queries. New dimensions are
added via `ALTER TABLE ADD COLUMN ... GENERATED ALWAYS AS ...` with no
application write-path change.

This replaces the existing `CompositeScope` string-join pattern, which is
irreversible, prefix-only, and compile-time-hardcoded. The existing
`CompositeScope` remains for file-backed stores where a flat path segment is
the correct representation.

### D3 — Canonical JSON serialization

All JSON serialization requiring deterministic output (scope columns,
scope_key uniqueness, payload comparisons) uses a recursive canonical
serializer (`modex_agent.utils.canonical_json`):

- Dict keys sorted at every nesting level
- Sets sorted and converted to lists
- Lists/tuples preserve element order (semantic) with recursive canonicalization
- `ensure_ascii=False`, compact separators
- Non-finite floats (`NaN`, positive infinity, negative infinity) are rejected
  with `allow_nan=False`

`RecordScope.canonical()` produces one stable string for any given set of
dimension values, regardless of field construction order.

### D4 — Store ABC convergence

The existing 15+ persistence ABCs are reorganized:

- **`MemoryStorage`** (god interface) → split into `MessageStore`, `KVStore`,
  `CursorStore`, `ArchiveStore` (4 single-responsibility ABCs).
- **`DeliveredIdTracker`** → merged into `InboxMQ` internal (not an independent
  ABC — delivered ID tracking is part of the inbox transaction).
- **`RegistryStore`** → deepened to `WorkspaceRegistryStore` (adds
  `last_active`, `display_name`, `workspace_id`; absorbs `RecentWorkspaces`).
- **`PoolSessionStore`** (concrete class) → extract `PoolRoutingStore` ABC.
- **`ExternalSessionStore`** (concrete class) → extract
  `ExternalSessionMapStore` ABC.
- **`LogStore`** → cancelled; archive channel logs are carried by the
  `ArchiveStore` DB table.
- **`InboxServer`** → evolves to `InboxMQ` (per inbox-mq-redesign PRD), adding
  `deliver()` sync method for CLI cross-process use.

`MediaStore`, `ToolOverflowStore`, `PrunedStorage` retain file-only
implementations — their semantics (binary streaming, large text chunking,
agent file-tool access) are incompatible with DB storage.

`TraceStore` and `ExperienceMetaStore` are marked DB-optional.

### D5 — Session message state machine + TTL

Session messages use a three-state lifecycle:

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

- `normal` + `pinned` are visible in active queries.
- `pinned` is exempt from automatic prune.
- `soft_deleted` is invisible to active queries but retained until TTL expiry.
- A background job physically deletes `soft_deleted` rows past their retention
  window.

Prune operation returns the pruned message content to the caller (archive
generator, pruned catalog writer, URB) in the same transaction as the
soft-delete — the existing file implementation achieved this implicitly by
discarding messages during overwrite; the DB implementation must do it
explicitly.

### D6 — Approval audit log

A new append-only `approval_audit_log` table records every approve/deny
decision with `turn_uuid`, `session_id`, `tool_name`, `tool_call_id`,
`decision`, `deny_reason`, `decided_at`, `decided_by`. A new
`ApprovalAuditStore` ABC provides `record()` and `query()`.

This closes a compliance gap: previously approval decisions lived only inside
`TurnSnapshot` and were overwritten by the next turn.

### D7 — ContextForkBuilder simplified to pure computation

`ContextForkBuilder` no longer writes fork XML files. Fork context is
constructed on-demand by querying the parent session's `MessageStore` for the
last N active messages, applying lossy compaction, and returning the XML
string directly. The in-memory cleanup registry and file persistence are
removed. `SessionGarbageCollector`'s artifact list drops from 10 to 9.

### D8 — Terminal state store removed

`JsonTerminalStateStore` and its `save_state()`/`load_state()` path in
`BaseTerminalManager` are dead code — production wiring never passes
`storage_dir`. The state store, its imports, and the architecture guard test
for the save/load seam are deleted.

### D9 — CLI (`modexctl`) uses `InboxMQ.deliver()`

`modexctl send` opens a short-lived SQLite connection to the target
workspace's `state.db` and calls `InboxMQ.deliver()` (sync method). The
existing `MODEX_INBOX_ROOT` environment variable still points to
`<workspace>/.modex/inbox`; the CLI derives `state.db` from its parent. No
new environment variables. CLI and framework server must switch to DB in the
same release — no dual-write window for inbox.

### D10 — PostgreSQL replaceability via domain ABCs

Each store ABC exposes domain operations (`enqueue`, `claim`, `ack`,
`save_turn`, `find_active`, `append_event`) — never SQL, ORM sessions, or
SQLite pragmas. SQLite and future PostgreSQL adapters run the same
conformance test suite. Backend-specific claim/lock implementations may
differ (SQLite single-writer vs PostgreSQL row-level locking) — the ABC
contract defines semantics, not mechanism.

### D11 — SQLite deployment and lifecycle

SQLite is an embedded library, not a server process. "Deployment" = one pip
dependency (`aiosqlite>=0.20.0`) for the async path; the CLI uses stdlib
`sqlite3` (no new dependency). No server installation, no port, no auth.

One `aiosqlite.Connection` per workspace `state.db`, managed by
`ConnectionManager` (PRAGMAs + migration runner + connection-level transaction
coordination). Adapters receive the manager, not an uncoordinated raw
connection; a manager-owned async lock prevents statements from another
adapter from interleaving inside a logical transaction. Opened during workspace
materialization, closed during eviction with `PRAGMA wal_checkpoint(TRUNCATE)`
before `close()`, after tasks that can write through its adapters have stopped
and final flushes have completed. The global registry DB connection is opened at
`BotService.initialize()` and closed last at `BotService.stop()`.

Migrations are plain SQL files shipped with the package, tracked by a
`schema_migrations` table. Applying one migration and recording its version is
one explicit transaction; migration scripts do not contain transaction-control
statements. No Alembic or ORM is introduced.

See `docs/design/hybrid-persistence/sqlite-deployment-and-lifecycle.md` for full design.

## Consequences

**Positive:**
- Inbox atomicity gap closed; CLI and server share one transaction boundary.
- One-active-turn invariant enforced by a partial unique index, not
  scan-then-write.
- Session/turn/inbox queries use indexes, not directory scans.
- Scope dimensions are data, not schema — adding a dimension is `ALTER TABLE`
  + `CREATE INDEX`, no application write-path change.
- True composite B-tree indexes on any scope dimension combination.
- Approval decisions are auditable.
- Workspace remains a portable, deletable, backup-able unit.
- File-based stores (knowledge, pruned, media, overflow) are untouched — no
  forced migration of human-editable or binary data.

**Negative:**
- Two persistence mechanisms (SQLite + files) increase operational complexity.
- Schema migrations must be managed (schema_migrations table + migration
  scripts).
- SQLite single-writer lock requires careful transaction design (short,
  bounded, no LLM/network/file work inside).
- Conformance test suite must cover file + SQLite + future PostgreSQL adapters.
- One-time inbox migration is a breaking cutover (no dual-write window).

**Neutral:**
- `CompositeScope` remains for file-backed stores; `RecordScope` serves
  DB-backed stores. Two scope systems coexist by design — they serve different
  storage models and should not be unified.
