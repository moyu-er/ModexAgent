# WebUI Transcript SQLite Persistence

**Status:** Approved for implementation (2026-07-15)

## Goal

Persist bot-project transcript/history events in the selected persistence backend
without changing WebUI, IM, session deletion, history replay, attachment-card, or
multi-workspace behavior.

When `persistence.backend` is `file`, transcript events remain JSONL files. When
it is `sqlite`, transcript events use the owning workspace's existing
`<workspace>/.modex/state.db`. Existing JSONL history is not imported, read as a
fallback, or dual-written.

## Ownership

Transcript events and their materialization are bot/WebUI business concepts.
The `TranscriptStore` interface, SQLite adapter, schema, and migration therefore
belong to `examples/bot_project`, not `src/modex_agent`.

The bot-owned migration shares the framework-owned workspace connection and
database but uses a separate migration namespace. The framework remains unaware
of WebUI event types and tables.

## Interface

`TranscriptStore` becomes fully asynchronous. Both adapters implement the same
interface:

- append one event;
- load one full session in insertion order;
- load all sessions for one conversation prefix in timestamp order;
- list full session IDs globally or by prefix;
- delete one full session or one complete prefix;
- report the latest persisted event timestamp;
- materialize incremental events into existing `MaterializedTurn` output.

The FILE adapter offloads blocking file operations with `asyncio.to_thread()`.
The SQLite adapter uses the workspace's `ConnectionManager`, including its lock,
transaction, and lifecycle rules. No adapter owns or closes that manager.

Workspace routing is provider-neutral. `WorkspaceScopedTranscriptStore` caches
one `TranscriptStore` adapter per workspace and obtains it through an injected
async resolver. It does not import a database adapter, inspect
`PersistenceBackend`, run migrations, or accept a database connection. The
default resolver is the pool-partitioned FILE adapter; database adapter
construction and schema preparation live in `bot.persistence.transcript`.
Adding another database provider therefore replaces persistence assembly and
its adapter, not WebUI, input-pipeline, garbage-collection, or workspace-routing
code.

## Schema

The bot migration creates one append-only event table:

```sql
CREATE TABLE bot_webui_transcript_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    session_prefix TEXT NOT NULL,
    pool_name      TEXT NOT NULL,
    agent_name     TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    turn_id        TEXT,
    timestamp_ms   INTEGER NOT NULL,
    payload_json   TEXT NOT NULL CHECK (json_valid(payload_json)),
    schema_version INTEGER NOT NULL DEFAULT 1
);
```

`event_id` is the stable tie-breaker for events with the same millisecond
timestamp. Frequently filtered identity fields are columns; event-specific data
remains versioned JSON so new WebUI event variants do not require table changes.

Indexes:

- `(session_id, event_id)` for full-session replay;
- `(session_prefix, timestamp_ms, event_id)` for conversation replay;
- `(pool_name, session_prefix, timestamp_ms, event_id)` for pool-filtered replay;
- partial `(turn_id, event_id)` where `turn_id IS NOT NULL` for turn diagnostics.

Exact equality on `session_prefix` prevents similar conversation IDs from
leaking into each other's history. Delete operations use the leading columns of
the first two indexes.

## Data Flow

The input pipeline and emitters await transcript append. Workspace routing is
resolved before selecting a physical adapter:

- FILE selects the workspace/pool JSONL adapter;
- SQLite selects the materialized workspace's SQLite adapter and records the
  resolved pool as an indexed column.

Concurrent first access to one workspace shares one resolver task. Cancelling
one waiter does not cancel shared materialization. Workspace eviction releases
the cached adapter before its persistence owner closes; a later access resolves
a fresh adapter rather than retaining a closed connection.

HTTP history reads resolve the requested workspace, obtain its materialized
transcript adapter, execute indexed queries, then reuse the existing event
decoder and `_materialize_events()` logic. The response format is unchanged.

Session cleanup awaits transcript deletion before deleting related session
artifacts. Workspace eviction first stops writers, then closes the shared
workspace connection through the existing owner lifecycle.

## Serialization

Rows reconstruct the same `ServerEvent` subclasses used by JSONL. The persisted
JSON includes the complete event shape for compatibility with the existing
`ServerEvent.from_dict()` decoder. Indexed columns are validated against the
event at append time and are authoritative for querying.

## Failure Semantics

Append failures remain non-fatal to an agent turn: the resilient adapter logs a
persistence failure and continues. Cancellation is never swallowed. Read,
list, materialization, and delete failures propagate to their HTTP or cleanup
caller so stale or incomplete results are not silently presented.

Provider adapters translate driver exceptions raised by append into
`TranscriptPersistenceError`. The resilient decorator catches only that domain
error and `OSError` from the FILE adapter; it has no dependency on SQLite or any
other database driver. Validation and programming errors continue to propagate.

SQLite uses the existing WAL, foreign-key, busy-timeout, synchronous, and
manager-lock settings. There is no background writer queue, so successful append
provides read-after-write consistency and shutdown has no unflushed transcript
buffer.

## Compatibility

External behavior remains unchanged:

- WebUI and IM messages use the same persistence stage;
- history ordering and materialized blocks remain the same;
- tool calls/results, reasoning, user messages, and attachment metadata survive
  round trips;
- session listing and deletion remain workspace-isolated;
- FILE remains a supported backend.

No JSONL-to-SQLite migration, runtime fallback read, dual write, or schema for
media bytes is included.

## Verification

Implementation is test-first and covers:

- FILE/SQLite CRUD and materialization conformance;
- Unicode and all structured event payload round trips;
- stable ordering for equal timestamps;
- exact prefix and pool filtering;
- main/subagent merge behavior;
- isolated full-session and prefix deletion;
- concurrent append without dropped events;
- workspace A/B database isolation;
- backend factory and lifecycle ownership;
- provider-neutral resolver deduplication, waiter cancellation, and eviction/reopen;
- provider error translation without database-driver imports in the interface module;
- WebUI history, session list/delete, approval resume, attachments, IM pipeline,
  and multi-workspace regression paths;
- `EXPLAIN QUERY PLAN` checks for the hot replay queries.
