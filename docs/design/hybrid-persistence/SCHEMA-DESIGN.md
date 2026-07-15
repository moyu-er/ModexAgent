# ModexAgent Persistence Architecture — Final Design

Snapshot: commit `51ef77d7567a40381796fb352bb814ce883e369f`, 2026-07-14.
ADRs: ADR-0023 (hybrid persistence), ADR-0015 (inbox, revised by PRD).
Status: design complete, pending implementation.

This document is the consolidated schema reference. See ADR-0023 for decision
rationale and CONTEXT.md for domain terms.

## Architecture

```
<home>/.modex/_registry/state.db        Global registry (workspaces, session→workspace map)

<workspace>/.modex/state.db             Per-workspace state DB
<workspace>/.modex/memory/...           Markdown knowledge, archive documents (files)
<workspace>/.modex/media/...            Attachment bytes (files)
<workspace>/.modex/overflow/...         Tool overflow chunks (files)
<workspace>/.modex/pruned/...           Pruned JSONL for agent file-tool access (files)
<workspace>/.modex/runtime_state/...    OUTPUT.md, trace JSONL, command artifacts (files)
<workspace>/.modex/experiences/...      EXPERIENCE.md trees (files)
<workspace>/.modex/config/...           Pool YAML, MCP JSON, prompts (files)
<workspace>/.modex/skills/...           Skill libraries + symlinks (files)
```

### Connection Prerequisites (every connection)

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA wal_autocheckpoint = 1000;
```

### Canonical JSON

All scope columns and payload columns requiring deterministic output use
`modex_agent.utils.canonical_json.canonical_json()` — recursive key sorting,
set-to-sorted-list conversion, compact separators, and `allow_nan=False` so
non-finite floats are rejected.

`RecordScope.canonical()` produces the stable string used as both the DB
`scope` JSON column value and the `scope_key` uniqueness column. Point
operations match the complete canonical key exactly; they never use partial
JSON matching. Aggregate operations use an explicit canonical owner key when
the adapter owns a family of record scopes. `scope` is the sole source for
generated dimensions, not the sole column an adapter writes; ordinary domain
keys and payload columns remain explicit.

Generated scope columns use the exact `RecordScope` field names. In particular,
the agent dimension is `agent_id`, never `agent` or `agent_name`.

---

## Registry DB Schema

File: `<home>/.modex/_registry/state.db`

```sql
CREATE TABLE schema_migrations (
    version      INTEGER PRIMARY KEY,
    description  TEXT    NOT NULL,
    applied_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE workspaces (
    workspace_id     TEXT    PRIMARY KEY,
    target_path      TEXT    NOT NULL UNIQUE,
    display_name     TEXT,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    last_active      TEXT    NOT NULL DEFAULT (datetime('now')),
    is_home          INTEGER NOT NULL DEFAULT 0,
    metadata_json    TEXT    CHECK (metadata_json IS NULL OR json_valid(metadata_json))
);

CREATE INDEX idx_workspaces_last_active ON workspaces (last_active);

CREATE TABLE session_workspace_map (
    session_prefix   TEXT    PRIMARY KEY,
    workspace_id     TEXT    NOT NULL REFERENCES workspaces(workspace_id) ON DELETE CASCADE,
    created_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_session_ws_workspace ON session_workspace_map (workspace_id);
```

Absorbs `GlobalWorkspaceStore` (known workspaces list) and `RecentWorkspaces`
(business layer, deprecated). `last_active` replaces both —
`SELECT ... ORDER BY last_active DESC LIMIT 20` yields recent workspaces.

---

## Workspace DB Schema

File: `<workspace>/.modex/state.db`

### Schema Versioning

```sql
CREATE TABLE schema_migrations (
    version      INTEGER PRIMARY KEY,
    description  TEXT    NOT NULL,
    applied_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
```

### Sessions

```sql
CREATE TABLE sessions (
    session_pk         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id         TEXT    NOT NULL UNIQUE,
    scope              TEXT    NOT NULL CHECK (json_valid(scope)),
    pool               TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    agent_id           TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.agent_id')) STORED,
    session_prefix     TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.session_prefix')) STORED,
    invocation_id      TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.invocation_id')) STORED,
    parent_session_id  TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.parent_session_id')) STORED,
    parent_session_pk  INTEGER REFERENCES sessions(session_pk) ON DELETE SET NULL,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    metadata_json      TEXT    CHECK (metadata_json IS NULL OR json_valid(metadata_json))
);

CREATE INDEX idx_sessions_pool_prefix ON sessions (pool, session_prefix) WHERE pool IS NOT NULL;
CREATE INDEX idx_sessions_prefix      ON sessions (session_prefix);
CREATE INDEX idx_sessions_parent      ON sessions (parent_session_id);
CREATE INDEX idx_sessions_pool_agent  ON sessions (pool, agent_id) WHERE pool IS NOT NULL;
```

### Pool Routing

```sql
CREATE TABLE pool_routing (
    session_prefix  TEXT    PRIMARY KEY,
    pool_name       TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_routing_pool ON pool_routing (pool);
```

### Inbox Topics

```sql
CREATE TABLE inbox_topics (
    topic_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_scope_key TEXT    NOT NULL COLLATE BINARY CHECK (json_valid(owner_scope_key)),
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    session_id      TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    state           TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'active', 'idle', 'expired')),
    created_at      REAL    NOT NULL,
    last_active     REAL    NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    consumer_task   TEXT,
    UNIQUE (scope_key),
    UNIQUE (topic_id, owner_scope_key, scope_key),
    UNIQUE (owner_scope_key, scope_key),
    UNIQUE (owner_scope_key, scope_key, session_id),
    CHECK (scope = scope_key),
    CHECK (COALESCE(json_type(scope, '$.session_id') = 'text', 0)),
    CHECK (json_extract(scope, '$.session_id') = session_id)
);

CREATE INDEX idx_topics_state       ON inbox_topics (state) WHERE state = 'pending';
CREATE INDEX idx_topics_last_active ON inbox_topics (last_active) WHERE state = 'idle';
CREATE INDEX idx_topics_owner       ON inbox_topics (owner_scope_key);
CREATE INDEX idx_topics_session     ON inbox_topics (session_id);
CREATE INDEX idx_topics_pool        ON inbox_topics (pool) WHERE pool IS NOT NULL;
```

### Inbox Messages

```sql
CREATE TABLE inbox_messages (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id                    INTEGER NOT NULL,
    owner_scope_key             TEXT    NOT NULL COLLATE BINARY,
    scope_key                   TEXT    NOT NULL COLLATE BINARY,
    session_id                  TEXT    NOT NULL,
    scope                       TEXT    NOT NULL CHECK (json_valid(scope)),
    pool                        TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    agent_id                    TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.agent_id')) STORED,
    session_prefix              TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.session_prefix')) STORED,
    invocation_id               TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.invocation_id')) STORED,
    parent_session_id           TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.parent_session_id')) STORED,
    message_id                  TEXT    NOT NULL,
    message_type                TEXT    NOT NULL
                                CHECK (message_type IN (
                                    'task_request', 'agent_message', 'agent_result',
                                    'subagent_result', 'external_input'
                                )),
    source_name                 TEXT    NOT NULL,
    source_kind                 TEXT    NOT NULL DEFAULT 'agent',
    content                     TEXT    NOT NULL,
    payload_json                TEXT    CHECK (payload_json IS NULL OR json_valid(payload_json)),
    envelope_session_id         TEXT,
    envelope_agent_session_id   TEXT,
    state                       TEXT    NOT NULL DEFAULT 'pending'
                                CHECK (state IN ('pending', 'consumed', 'expired')),
    seq                         INTEGER NOT NULL,
    created_at                  REAL    NOT NULL,
    consumed_at                 REAL,
    UNIQUE (scope_key, message_id),
    FOREIGN KEY (topic_id, owner_scope_key, scope_key)
        REFERENCES inbox_topics(topic_id, owner_scope_key, scope_key) ON DELETE CASCADE,
    CHECK (scope = scope_key),
    CHECK (COALESCE(json_type(scope, '$.session_id') = 'text', 0)),
    CHECK (json_extract(scope, '$.session_id') = session_id)
);

CREATE INDEX idx_messages_topic_state_seq ON inbox_messages (topic_id, state, seq);
CREATE INDEX idx_messages_owner_session   ON inbox_messages (owner_scope_key, session_id);
CREATE INDEX idx_messages_scope_session   ON inbox_messages (scope_key, session_id);
CREATE INDEX idx_messages_pool_session    ON inbox_messages (pool, session_id) WHERE pool IS NOT NULL;
CREATE INDEX idx_messages_pool_agent      ON inbox_messages (pool, agent_id) WHERE pool IS NOT NULL;
CREATE INDEX idx_messages_parent          ON inbox_messages (parent_session_id);
```

### Inbox Delivered IDs (internal to InboxMQ)

```sql
CREATE TABLE inbox_delivered_ids (
    owner_scope_key TEXT    NOT NULL COLLATE BINARY,
    scope_key       TEXT    NOT NULL COLLATE BINARY,
    session_id      TEXT    NOT NULL,
    message_id      TEXT    NOT NULL,
    delivered_at    REAL    NOT NULL,
    PRIMARY KEY (scope_key, message_id),
    FOREIGN KEY (owner_scope_key, scope_key, session_id)
        REFERENCES inbox_topics(owner_scope_key, scope_key, session_id) ON DELETE CASCADE
);
```

### Inbox Dead Letter

```sql
CREATE TABLE inbox_dead_letter (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_scope_key     TEXT    NOT NULL COLLATE BINARY,
    scope_key           TEXT    NOT NULL COLLATE BINARY,
    session_id          TEXT    NOT NULL,
    scope               TEXT    NOT NULL CHECK (json_valid(scope)),
    pool                TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    message_id          TEXT    NOT NULL,
    message_type        TEXT    NOT NULL,
    source_name         TEXT    NOT NULL,
    content             TEXT    NOT NULL,
    payload_json        TEXT,
    expired_reason      TEXT    NOT NULL,
    expired_at          REAL    NOT NULL,
    original_created_at REAL    NOT NULL,
    UNIQUE (scope_key, message_id),
    FOREIGN KEY (owner_scope_key, scope_key)
        REFERENCES inbox_topics(owner_scope_key, scope_key) ON DELETE CASCADE,
    CHECK (scope = scope_key),
    CHECK (COALESCE(json_type(scope, '$.session_id') = 'text', 0)),
    CHECK (json_extract(scope, '$.session_id') = session_id)
);

CREATE INDEX idx_dead_letter_owner_session ON inbox_dead_letter (owner_scope_key, session_id);
CREATE INDEX idx_dead_letter_pool_session ON inbox_dead_letter (pool, session_id) WHERE pool IS NOT NULL;
```

`owner_scope_key` is the exact canonical scope supplied when constructing an
inbox adapter. `scope_key` is that scope merged with the addressed
`session_id`. Framework queries use `scope_key = ?` for one session and
`owner_scope_key = ?` for owner-wide listings or retention. `pool` remains an
optional generated business dimension and is never required for framework
identity.

### Turn Snapshots

```sql
CREATE TABLE turn_snapshots (
    snapshot_pk       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT    NOT NULL,
    agent_id          TEXT    NOT NULL,
    turn_id           TEXT    NOT NULL,
    scope             TEXT    NOT NULL CHECK (json_valid(scope)),
    pool              TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    session_prefix    TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.session_prefix')) STORED,
    parent_session_id TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.parent_session_id')) STORED,
    agent_kind        TEXT    NOT NULL DEFAULT 'react',
    phase             TEXT    NOT NULL
                      CHECK (phase IN ('running', 'suspended', 'completed', 'cancelled', 'error')),
    reason            TEXT,
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL,
    schema_version    INTEGER NOT NULL DEFAULT 1,
    payload_json      TEXT    NOT NULL CHECK (json_valid(payload_json)),
    UNIQUE (session_id, agent_id, turn_id)
);

CREATE UNIQUE INDEX idx_turn_active_unique
    ON turn_snapshots (agent_id, session_id)
    WHERE phase IN ('running', 'suspended');

CREATE INDEX idx_turn_session      ON turn_snapshots (session_id);
CREATE INDEX idx_turn_pool_session ON turn_snapshots (pool, session_id) WHERE pool IS NOT NULL;
CREATE INDEX idx_turn_parent       ON turn_snapshots (parent_session_id);
CREATE INDEX idx_turn_phase        ON turn_snapshots (phase) WHERE phase IN ('running', 'suspended');
CREATE INDEX idx_turn_created      ON turn_snapshots (created_at);
```

### Approval Audit Log

```sql
CREATE TABLE approval_audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_uuid       TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    agent_id        TEXT    NOT NULL,
    turn_id         TEXT    NOT NULL,
    tool_name       TEXT    NOT NULL,
    tool_call_id    TEXT,
    decision        TEXT    NOT NULL CHECK (decision IN ('approved', 'denied')),
    deny_reason     TEXT,
    decided_at      REAL    NOT NULL,
    decided_by      TEXT    NOT NULL DEFAULT 'user'
);

CREATE INDEX idx_approval_session ON approval_audit_log (session_id, decided_at);
CREATE INDEX idx_approval_turn    ON approval_audit_log (turn_uuid);
CREATE INDEX idx_approval_pool    ON approval_audit_log (pool, decided_at) WHERE pool IS NOT NULL;
```

### Todos

```sql
CREATE TABLE todos (
    session_id      TEXT    PRIMARY KEY,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    items_json      TEXT    NOT NULL CHECK (json_valid(items_json)),
    updated_at      REAL    NOT NULL
);

CREATE INDEX idx_todos_pool ON todos (pool) WHERE pool IS NOT NULL;
```

### Transcript Events (optional DB migration)

```sql
CREATE TABLE transcript_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    session_prefix  TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    turn_id         TEXT,
    timestamp_ms    INTEGER NOT NULL,
    payload_json    TEXT    NOT NULL CHECK (json_valid(payload_json)),
    schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_transcript_session      ON transcript_events (session_id, id);
CREATE INDEX idx_transcript_prefix_time  ON transcript_events (session_prefix, timestamp_ms);
CREATE INDEX idx_transcript_pool_session ON transcript_events (pool, session_id);
```

### Attachments Metadata

```sql
CREATE TABLE attachments (
    attachment_id   TEXT    PRIMARY KEY,
    session_id      TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    name            TEXT    NOT NULL,
    mime            TEXT,
    size            INTEGER NOT NULL,
    kind            TEXT    NOT NULL CHECK (kind IN ('image', 'extractable_document', 'other')),
    locator         TEXT    NOT NULL CHECK (locator IN ('media', 'workspace')),
    relative_path   TEXT    NOT NULL,
    created_at      REAL    NOT NULL
);

CREATE INDEX idx_attachments_session ON attachments (session_id);
CREATE INDEX idx_attachments_pool    ON attachments (pool);
```

### Memory — Session Messages (with state machine)

```sql
CREATE TABLE memory_session_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key       TEXT NOT NULL,
    scope           TEXT NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    session_id      TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.session_id')) STORED,
    user_id         TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.user_id')) STORED,
    agent_id        TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.agent_id')) STORED,
    seq             INTEGER NOT NULL,
    role            TEXT    NOT NULL,
    message_json    TEXT    NOT NULL CHECK (json_valid(message_json)),
    created_at      REAL    NOT NULL,
    state           TEXT    NOT NULL DEFAULT 'normal'
                    CHECK (state IN ('normal', 'pinned', 'soft_deleted')),
    deleted_at      REAL,
    UNIQUE (scope_key, seq),
    CHECK ((state = 'soft_deleted') = (deleted_at IS NOT NULL))
);

CREATE INDEX idx_memory_session_active
    ON memory_session_messages (scope_key, seq)
    WHERE state IN ('normal', 'pinned');

CREATE INDEX idx_memory_session_ttl
    ON memory_session_messages (deleted_at)
    WHERE state = 'soft_deleted';

CREATE INDEX idx_memory_session_state
    ON memory_session_messages (scope_key, state);

CREATE INDEX idx_memory_session_pool
    ON memory_session_messages (pool) WHERE pool IS NOT NULL;
```

The `content` column was removed in migration 002 — it duplicated the
`content` field inside `message_json`. The adapter reads only
`message_json` (via `json.loads`), so `content` was write-only dead
storage that roughly doubled per-row size for text-heavy conversations.

### Memory — KV Store

```sql
CREATE TABLE memory_kv (
    scope_key       TEXT    NOT NULL,
    key             TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    value_json      TEXT    NOT NULL CHECK (json_valid(value_json)),
    updated_at      REAL    NOT NULL,
    PRIMARY KEY (scope_key, key)
);

CREATE INDEX idx_memory_kv_pool ON memory_kv (pool) WHERE pool IS NOT NULL;
```

### Memory — Cursors

```sql
CREATE TABLE memory_cursors (
    scope_key       TEXT    NOT NULL,
    cursor_name     TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    cursor_value    INTEGER NOT NULL,
    updated_at      REAL    NOT NULL,
    PRIMARY KEY (scope_key, cursor_name)
);
```

### Memory — Revisions

```sql
CREATE TABLE memory_revisions (
    scope_key       TEXT    PRIMARY KEY,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    message_count   INTEGER NOT NULL DEFAULT 0,
    version         INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL    NOT NULL
);
```

### Memory — Archive State + Entries

```sql
CREATE TABLE memory_archive_state (
    scope_key       TEXT    PRIMARY KEY,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    pool            TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    next_archive_id INTEGER NOT NULL DEFAULT 1,
    state_json      TEXT    CHECK (state_json IS NULL OR json_valid(state_json)),
    updated_at      REAL    NOT NULL
);

CREATE TABLE memory_archive_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key       TEXT    NOT NULL,
    scope           TEXT    NOT NULL CHECK (json_valid(scope)),
    archive_id      INTEGER NOT NULL,
    channel         TEXT    NOT NULL,
    summary         TEXT,
    created_at      REAL    NOT NULL,
    UNIQUE (scope_key, archive_id, channel)
);

CREATE INDEX idx_archive_entries_scope ON memory_archive_entries (scope_key, archive_id);
```

Archive Markdown files (`{archive_id}/{channel}.md`) remain on the filesystem.

### External Provider Session Map

```sql
CREATE TABLE external_session_map (
    modex_session_id    TEXT    PRIMARY KEY,
    provider_session_id TEXT    NOT NULL,
    provider_kind       TEXT    NOT NULL CHECK (provider_kind IN ('pi', 'opencode')),
    scope               TEXT    NOT NULL CHECK (json_valid(scope)),
    pool                TEXT    GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED,
    last_committed_at   REAL    NOT NULL,
    invalidated         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_external_pool ON external_session_map (pool) WHERE pool IS NOT NULL;
```

### Workspace Metadata

```sql
CREATE TABLE workspace_meta (
    key         TEXT    PRIMARY KEY,
    value_json  TEXT    NOT NULL CHECK (json_valid(value_json)),
    updated_at  REAL    NOT NULL
);
```

---

## What Stays as Files

| artifact | reason |
|---|---|
| `config/pools/*/pool.yml`, `config/model.yml`, `config/mcp/*.json` | human-authored, source-controlled, WebUI-edited |
| `agents/*.md` | human-authored prompts |
| `skills/<pool>/<agent>/<name>/SKILL.md` | human-authored, symlinked |
| `memory/<pool>/knowledge/*.md` (`SOUL.md`, `USER.md`, `MEMORY.md`) | human-edited, file-tool-edited |
| `experiences/<pool>/<agent>/*/EXPERIENCE.md` | agent-generated Markdown, file-tool-edited |
| `media/uploads/<session>/<attachment_id>` | binary streams, size-budgeted |
| `overflow/tool_overflow/<session>/<tool_call>/` | large text chunks, streamed |
| `runtime_state/<pool>/output/<session>/OUTPUT.md` | tool-produced, path-injected into prompts |
| `runtime_state/<pool>/trace/<session>/operations.jsonl` | append-only telemetry (optional DB migration) |
| `pruned/<pool>/<session_id>/` | agent file-tool access — must remain files |
| `external/pi-session.jsonl` | provider-owned (Pi/OpenCode) |
| Logs | log files |

---

## Store ABC Final List

| # | ABC | Layer | Module | DB | Notes |
|---|---|---|---|---|---|
| 1 | `WorkspaceRegistryStore` | framework | `workspace/` | ✅ | deepened `RegistryStore`; absorbs `RecentWorkspaces` |
| 2 | `SessionStore` | framework | `core/` | ✅ | removed `index_dir` param |
| 3 | `PoolRoutingStore` | framework | `multi_agent/` | ✅ | extracted from `PoolSessionStore` |
| 4 | `ExternalSessionMapStore` | framework | `agents/external_coding/` | ✅ | extracted from `ExternalSessionStore` |
| 5 | `TurnStateStore` | framework | `runtime/` | ✅ | — |
| 6 | `TodoStore` | framework | `runtime/` | ✅ | — |
| 7 | `InboxMQ` | framework | `multi_agent/inbox/` | ✅ | `deliver()` sync method; absorbs `DeliveredIdTracker` |
| 8 | `TraceStore` | framework | `trace/` | ⚠️ optional | — |
| 9 | `MessageStore` | framework | `memory/core/` | ✅ | split from `MemoryStorage`; state machine + TTL |
| 10 | `KVStore` | framework | `memory/core/` | ✅ | split; URB is consumer |
| 11 | `CursorStore` | framework | `memory/core/` | ✅ | split; cursor separated from state.json |
| 12 | `ArchiveStore` | framework | `memory/core/` | ✅ | archive metadata in DB; Markdown files retained |
| 13 | `PrunedStorage` | framework | `memory/pruned/` | ❌ file | agent file-tool access |
| 14 | `ExperienceMetaStore` | framework | `core/experience/` | ⚠️ optional | — |
| 15 | `MediaStore` | framework | `media/` | ❌ file | binary streaming; no SQL |
| 16 | `ToolOverflowStore` | framework | `tools/overflow/` | ❌ file | large text chunking |
| 17 | `ApprovalAuditStore` | framework | `runtime/` | ✅ | new; immutable audit log |
| 18 | `TranscriptStore` | business | — | business | WebUI-specific |
| — | `SessionArtifactCleaner` | framework | `persistence/` | — | new; DB + file cascade delete |
| — | `RecordScope` / `Scope` | framework | `core/` | — | refactored |
| — | `canonical_json` | framework | `utils/` | — | new |
| — | `persistence/` module | framework | `persistence/` | — | DB adapters + connection management |
| — | `modexctl` CLI | framework | `src/modexctl/` | — | calls `InboxMQ.deliver()` |
| — | `ContextForkBuilder` | framework | `multi_agent/` | — | simplified to pure computation |
| — | Terminal state store | framework | `tools/terminal/` | ❌ removed | dead code deleted |

**Deprecated/removed:**
- `DeliveredIdTracker` → merged into `InboxMQ`
- `MemoryStorage` → split into #9-12
- `LogStore` → cancelled (archive channel logs in `ArchiveStore`)
- `RecentWorkspaces` → absorbed into `WorkspaceRegistryStore`
- `RegistryStore` → deepened to `WorkspaceRegistryStore`
- Fork XML files → `ContextForkBuilder` simplified to pure computation
- `JsonTerminalStateStore` → deleted (dead code)
- `InMemoryRuntimeContextStore` → not in persistence layer (pure memory)

**SessionGarbageCollector artifacts: 10 → 9** (fork_contexts removed).

---

## Cleanup Mechanisms

| Store | Mechanism | Trigger | Implementation |
|---|---|---|---|
| Session messages | prune → soft_deleted | `cleanup_session()` | `UPDATE state='soft_deleted'` in same txn as content return |
| Session messages | TTL physical delete | background job | `DELETE WHERE state='soft_deleted' AND deleted_at < ?` |
| Archive entries | max_entries / max_age_days | `scan_once()` | `DELETE FROM memory_archive_entries WHERE ...` + delete Markdown dirs |
| Knowledge | max_memory_chars | `scan_once()` | truncate file content (file system) |
| Pruned | `prune_oldest(keep_count)` | explicit call | delete oldest JSONL + index entries (file system) |
| URB | `_enforce_limits` max_entries | each append/upsert | FIFO from list head (KVStore) |
| Turn snapshots | completed retention | background job | `DELETE WHERE phase IN ('completed','cancelled','error') AND created_at < ?` |
| Inbox messages | `reap_expired()` | poller tick | `DELETE WHERE state='expired' AND created_at < ?` |
| Inbox dead_letter | TTL | background job | `DELETE WHERE expired_at < ?` |
| Inbox delivered_ids | TTL | background job | `DELETE WHERE delivered_at < ?` |
| Approval audit | TTL (optional) | background job | `DELETE WHERE decided_at < ?` |
| Overflow | `clean(kept_call_ids)` | session cleanup | delete dirs not in kept list (file system) |
| Session cascade | `SessionArtifactCleaner` | WebUI delete / orphan scan | DB rows + file dirs coordinated |

---

## Migration Sequence

This sequence targets newly created registry and workspace databases. The
release does not import existing file-backed data and does not transform an
earlier SQLite schema; users opt into the selected backend with a fresh DB.

1. Define typed identity/scope models + `canonical_json` + conformance test contracts
2. SQLite connection/migration infrastructure (per-workspace + registry)
3. Inbox + `modexctl` (P0 — breaking cutover; no importer, shadow read, or dual write)
4. Turn snapshots + approval audit log (P0)
5. Sessions, pool routing, workspace registry, external session map, todos (P1)
6. Transcript + attachment metadata (P2 — after durability decision)
7. Memory session/KV/cursors + archive metadata (P2)
8. Trace (P3 — optional, measure first)
