# SQLite Deployment and Lifecycle Design

ADR: ADR-0023 (D1)
Status: design (2026-07-14)

## What SQLite Actually Is

SQLite is **not a server**. It is a C library embedded directly in the
application process. There is:

- No separate process to install, start, or stop
- No port to configure
- No authentication or user management
- No network protocol
- No configuration file

The entire "deployment" is: the Python stdlib `sqlite3` module (which bundles
SQLite on Windows) opens a file on disk. That file *is* the database.

```
Python process
  └─ sqlite3 module (stdlib, bundles SQLite C library)
       └─ opens <workspace>/.modex/state.db (a file on disk)
            ├─ state.db        (main database file)
            ├─ state.db-wal    (write-ahead log, auto-created in WAL mode)
            └─ state.db-shm    (shared memory index, auto-created in WAL mode)
```

The `-wal` and `-shm` files are created automatically when WAL mode is
enabled and deleted on clean checkpoint. They are not separate databases —
they are transient files managed by SQLite. On crash, they are replayed
automatically on next open.

**Verified environment:**
- Python 3.12 (project requirement: `requires-python = ">=3.12"`)
- SQLite 3.51.0 (verified: `python -c "import sqlite3; print(sqlite3.sqlite_version)"`)
- STORED generated columns require 3.31+ (Jan 2020) — satisfied by 3.51.0
- WAL mode requires 3.7+ (2010) — satisfied

## Dependencies

### New dependency: `aiosqlite`

```
# pyproject.toml [project.dependencies]
"aiosqlite>=0.20.0,<1",
```

**Why `aiosqlite` (not raw stdlib + `asyncio.to_thread`):**

The framework is fully async. SQLite's stdlib `sqlite3` is synchronous. Two
options exist:

| Option | Pros | Cons |
|---|---|---|
| `aiosqlite` | Purpose-built async wrapper; connection lifecycle, cursor management, transaction context managers built in; well-maintained (~1K lines) | One new dependency |
| `sqlite3` + `asyncio.to_thread` | No new dependency | Manual thread management; no async transaction context managers; boilerplate for every query; easy to forget `to_thread` and block the event loop |

`aiosqlite` runs each connection on a dedicated thread with an async queue.
All operations on a connection are serialized by that thread, but different
connections to the same DB file can run concurrently (WAL allows concurrent
readers + one writer).

Serialization by the worker thread is not a transaction coordinator: queued
statements from different adapters can otherwise interleave between `BEGIN` and
`COMMIT`. `ConnectionManager` therefore owns an async transaction lock. Every
adapter receives the manager and performs SQL through its operation/transaction
interface rather than managing transactions on the raw connection.

**What `aiosqlite` does NOT do:**
- It is not an ORM
- It does not add a query builder
- It does not add migrations
- It does not add connection pooling (we manage that ourselves)

It is a thin async wrapper: `await conn.execute("SQL")` instead of
`conn.execute("SQL")` in a thread.

### CLI path: stdlib `sqlite3` (no new dependency)

`modexctl` is a synchronous CLI. It uses Python's built-in `sqlite3` module
directly — no `aiosqlite` needed. The CLI opens a short-lived connection,
executes one transaction, and closes. This is the correct pattern for a
one-shot process.

### No other dependencies needed

- No SQLAlchemy (rejected: ORM leakage per ADR-0023 D10)
- No Alembic (rejected: we use a simple migration runner, see below)
- No sqlite-utils (rejected: we need control over PRAGMAs and connection lifecycle)

## Module Structure

```
src/modex_agent/persistence/
├── __init__.py
├── connection.py          # ConnectionManager — open/close/PRAGMA lifecycle
├── migration.py           # MigrationRunner — version-tracked SQL migration
├── registry_db.py         # RegistryPersistenceManager — global registry DB
├── workspace_db.py        # WorkspacePersistenceManager — per-workspace DB
├── adapters/              # SQLite implementations of store ABCs
│   ├── __init__.py
│   ├── session_store.py
│   ├── pool_routing_store.py
│   ├── turn_state_store.py
│   ├── todo_store.py
│   ├── inbox_mq.py
│   ├── message_store.py
│   ├── kv_store.py
│   ├── cursor_store.py
│   ├── archive_store.py
│   ├── external_session_map.py
│   ├── workspace_registry.py
│   └── approval_audit_store.py
└── migrations/            # SQL migration files (shipped with the package)
    ├── workspace/
    │   └── 001_initial.sql
    └── registry/
        └── 001_initial.sql
```

### Why `persistence/` is a separate module

All DB adapters share:
- Connection management (WAL, PRAGMAs, busy_timeout)
- Migration framework (schema_migrations table, version tracking)
- Transaction patterns (BEGIN IMMEDIATE, retry on BUSY)

These are cross-cutting infrastructure concerns. Putting them in each store
module would duplicate the connection/PRAGMA/migration code. The `persistence/`
module is the single home for this infrastructure; individual store ABCs
(`SessionStore`, `InboxMQ`, etc.) remain in their domain modules
(`core/`, `multi_agent/inbox/`, etc.) — only their SQLite *implementations*
live in `persistence/adapters/`.

## Connection Management

### `ConnectionManager` (framework layer)

```python
class ConnectionManager:
    """Manages one aiosqlite connection to a workspace state.db.

    Lifecycle:
    - open(): set PRAGMAs, run migrations
    - execute/executemany/commit: delegate to aiosqlite
    - close(): WAL checkpoint TRUNCATE, then close

    One ConnectionManager per workspace DB.
    All async DB operations in that workspace go through this one connection.
    """

    def __init__(self, db_path: Path, database_kind: DatabaseKind) -> None: ...

    async def open(self) -> None:
        """Open connection, set PRAGMAs, run pending migrations."""
        self._conn = await aiosqlite.connect(str(self._db_path))
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        await self._conn.commit()
        await MigrationRunner(self, self._database_kind).run_pending()

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False):
        """Hold the connection transaction lock through commit or rollback."""
        ...

    async def close(self) -> None:
        """Checkpoint WAL to main DB, then close connection."""
        if self._conn is not None:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await self._conn.close()
            self._conn = None

    # Adapters depend on ConnectionManager's typed execute/query/transaction
    # interface. The raw connection remains an implementation detail.
```

`DatabaseKind` is a closed enum with `WORKSPACE` and `REGISTRY`. It selects the
matching packaged migration stream; the DB path does not implicitly decide
which schema to apply.

The transaction lock is held only around bounded SQLite work. LLM calls,
network calls, file I/O, and waits on other runtime modules are forbidden while
it is held. Nested transactions are not part of the v1 interface.

### One connection per workspace

A single `aiosqlite.Connection` serves all DB operations for one workspace.
`aiosqlite` runs the connection on a dedicated thread — operations are
serialized on that connection, but SQLite operations for our data sizes are
sub-millisecond. The serialization is acceptable for a local single-process
bot.

If profiling shows contention (e.g., inbox consume blocking session message
loads), we can add a read-only connection pool. But this is premature
optimization — measure first.

### Why not multiple connections per workspace

- Each `aiosqlite.Connection` uses a thread + queue. More connections = more
  threads, more memory, more context switching.
- SQLite WAL allows concurrent readers + one writer. With one connection,
  reads and writes are serialized by the connection thread. With multiple
  connections, reads can proceed concurrently while a write is in progress —
  but our write transactions are short (no LLM/network/file work inside).
- The complexity of managing a read pool + write connection is not justified
  until profiling proves a bottleneck.

### CLI connection (sync, short-lived)

```python
# modexctl/main.py — CLI path, no aiosqlite
import sqlite3

def _send(to: str, content: str) -> None:
    state_db = _resolve_state_db_path()
    conn = sqlite3.connect(str(state_db), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("BEGIN IMMEDIATE")
        # ... topic upsert + message insert ...
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
```

The CLI's short-lived connection coexists with the framework's long-lived
connection via WAL. Both can write (not simultaneously — WAL allows one
writer at a time, `BEGIN IMMEDIATE` + `busy_timeout` handles contention).

## Migration System

### `MigrationRunner` (framework layer)

```python
class MigrationRunner:
    """Version-tracked SQL migration runner.

    Migrations are plain SQL files shipped with the package:
      src/modex_agent/persistence/migrations/{scope}/NNN_description.sql

    Each migration runs in a transaction. The schema_migrations table
    tracks applied versions.
    """

    def __init__(self, connection: ConnectionManager, database_kind: DatabaseKind) -> None:
        self._connection = connection
        self._database_kind = database_kind

    async def run_pending(self) -> None:
        await self._ensure_table()
        current = await self._current_version()
        for migration in self._pending_migrations(current):
            await self._apply(migration)

    async def _ensure_table(self) -> None:
        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version      INTEGER PRIMARY KEY,
                description  TEXT    NOT NULL,
                applied_at   TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)

    async def _apply(self, migration: MigrationFile) -> None:
        sql = migration.read_sql()
        async with self._connection.transaction(immediate=True) as tx:
            await tx.execute_script_statements(sql)
            await tx.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?)",
                (migration.version, migration.description),
            )
```

### Migration file format

```sql
-- src/modex_agent/persistence/migrations/workspace/001_initial.sql
-- All tables for the initial workspace schema.

CREATE TABLE sessions (...);
CREATE TABLE inbox_topics (...);
CREATE TABLE inbox_messages (...);
-- ... (all tables from SCHEMA-DESIGN.md)
```

Each file is a plain SQL script containing schema statements only; migration
files must not contain `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`, or
`RELEASE`. The runner executes the statements and inserts the migration row in
one explicit transaction, rolling back both on failure. It must not rely on
stdlib/`aiosqlite` `executescript()` to create that transaction, because
`executescript()` has its own transaction-boundary behavior. Adding a migration
means creating `002_description.sql`; no framework DSL is introduced.

### Packaging migrations

Migration SQL files are included in the wheel via hatch build config:

```toml
# pyproject.toml
[tool.hatch.build.targets.wheel.force-include]
"src/modex_agent/persistence/migrations" = "modex_agent/persistence/migrations"
```

This ensures migrations ship with `pip install`.

## Bot Lifecycle Integration

### Startup sequence

The persistence layer integrates at the `WorkspaceRegistry.materialize()`
point — when a workspace's resources are built, the DB connection is opened.

```
BotService.initialize()
  └─ workspace_stack = build_workspace_stack(...)
       └─ registry = WorkspaceRegistry(factory=PoolResourceFactory())

  └─ registry.materialize(home_context)
       └─ factory.materialize(ctx)  # bot/workspace/bundle/wiring.py
            └─ PoolWorkspaceResources built
                 └─ PersistenceManager opened for this workspace  ← NEW
                      ├─ ConnectionManager.open()
                      │    ├─ aiosqlite.connect(state.db)
                      │    ├─ PRAGMA journal_mode=WAL, foreign_keys=ON, ...
                      │    └─ MigrationRunner.run_pending()
                      └─ Store adapters constructed with ConnectionManager
```

### `WorkspacePersistenceManager` (framework layer)

```python
class WorkspacePersistenceManager:
    """Owns the DB connection for one workspace.

    Created during workspace materialization.
    Closed during workspace eviction.
    """

    def __init__(self, db_path: Path) -> None:
        self._conn_mgr = ConnectionManager(db_path, DatabaseKind.WORKSPACE)
        self._stores: dict[str, Any] = {}  # lazy-initialized store adapters

    async def open(self) -> None:
        await self._conn_mgr.open()

    async def close(self) -> None:
        await self._conn_mgr.close()

    def inbox_mq(self) -> SqliteInboxMQ:
        if "inbox_mq" not in self._stores:
            self._stores["inbox_mq"] = SqliteInboxMQ(self._conn_mgr)
        return self._stores["inbox_mq"]

    def session_store(self) -> SqliteSessionStore:
        if "session_store" not in self._stores:
            self._stores["session_store"] = SqliteSessionStore(self._conn_mgr)
        return self._stores["session_store"]

    # ... one method per store adapter ...
```

### Registry persistence (global, service-level)

```python
class RegistryPersistenceManager:
    """Owns the global registry DB connection.

    Created at BotService.initialize(), before workspace materialization.
    Closed at BotService.stop(), after all workspaces are evicted.
    """

    def __init__(self, registry_db_path: Path) -> None:
        self._conn_mgr = ConnectionManager(registry_db_path, DatabaseKind.REGISTRY)

    async def open(self) -> None:
        await self._conn_mgr.open()  # runs registry migrations

    async def close(self) -> None:
        await self._conn_mgr.close()

    def workspace_registry_store(self) -> SqliteWorkspaceRegistryStore: ...
    def session_workspace_map_store(self) -> SqliteSessionWorkspaceMapStore: ...
```

### Shutdown sequence

Integrates into the existing `BotService.stop()` flow:

```
BotService.stop()
  ├─ _shutdown_event.set()
  ├─ cancel maintenance_task
  ├─ cancel router_task
  │
  ├─ workspace_stack.registry.evict_all()        ← EXISTING
  │    └─ for each materialized workspace:
  │         └─ factory.evict(resources)           ← EXISTING
  │              └─ _stop_resources(resources)    ← EXISTING
  │                   ├─ stop producers/pollers/pools/broker/terminals
  │                   ├─ complete final store flushes
  │                   └─ persistence_mgr.close()  ← NEW (DB closes last in workspace)
  │                        ├─ PRAGMA wal_checkpoint(TRUNCATE)
  │                        └─ connection.close()
  │
  ├─ mcp_registry.shutdown()                      ← EXISTING
  ├─ input_adapter.stop()                         ← EXISTING
  │
  └─ registry_persistence.close()                 ← NEW (last — after all workspaces)
       ├─ PRAGMA wal_checkpoint(TRUNCATE)
       └─ connection.close()
```

**Key ordering principle:** first stop every task/resource that can initiate a
workspace DB write, then complete final store flushes, then checkpoint and close
that workspace DB. The Registry DB closes last, after all workspaces are
evicted. Closing a DB before its producers would make final writes race a closed
connection.

### Where persistence managers are held

```python
# bot/workspace/bundle/wiring.py — PoolWorkspaceResources (existing, extended)
@dataclass
class PoolWorkspaceResources:
    # ... existing fields ...
    persistence: WorkspacePersistenceManager  # NEW

# bot/service/core.py — BotService (existing, extended)
class BotService:
    def __init__(self, ...):
        # ... existing ...
        self._registry_persistence: RegistryPersistenceManager | None = None  # NEW

    async def initialize(self):
        # ... existing ...
        # Open registry DB BEFORE workspace materialization
        self._registry_persistence = RegistryPersistenceManager(registry_path)
        await self._registry_persistence.open()

    async def stop(self):
        # ... existing evict_all ...
        # Close registry DB AFTER all workspaces evicted
        if self._registry_persistence is not None:
            await self._registry_persistence.close()
```

### Workspace eviction (multi-live)

When `WorkspaceRegistry.evict()` drops a workspace (LRU pressure or explicit
eviction), `factory.evict(resources)` calls `_stop_resources(resources)`,
which now also calls `resources.persistence.close()`:

```python
# bot/workspace/bundle/wiring.py — _stop_resources (existing, extended)
async def _stop_resources(resources: PoolWorkspaceResources) -> None:
    # ... existing: background.stop(), terminals, pools, broker ...
    # Close DB connection LAST (after all stores have flushed)
    with contextlib.suppress(BaseException):
        await resources.persistence.close()
```

**WAL recovery on crash:** if the process is killed without `close()`, WAL
files remain. On next `open()`, SQLite automatically replays the WAL. No
data loss for committed transactions; uncommitted transactions are rolled
back. This is SQLite's built-in crash recovery — no application code needed.

## IOC Factory Wiring

### New IOC config section

```python
# ioc/configs/persistence.py
class PersistenceBackend(StrEnum):
    FILE = "file"
    SQLITE = "sqlite"


class PersistenceConfig(BaseModel):
    """Persistence layer configuration."""
    model_config = ConfigDict(frozen=True)

    backend: PersistenceBackend = PersistenceBackend.SQLITE
    # SQLite-specific (ignored when backend is not SQLITE)
    busy_timeout_ms: int = 5000
    wal_autocheckpoint: int = 1000
```

### Factory selection

```python
# ioc/factories/persistence.py
def build_inbox_mq(
    config: PersistenceConfig,
    persistence_mgr: WorkspacePersistenceManager | None,
    workspace_paths: WorkspacePaths,
) -> InboxMQ:
    if persistence_mgr is not None and config.backend is PersistenceBackend.SQLITE:
        return persistence_mgr.inbox_mq()
    # Fallback: file implementation (existing LocalFileInboxMQ)
    return LocalFileInboxMQ(workspace_paths.inbox_dir)
```

Each store factory follows the same pattern: if DB persistence is configured
and the workspace has a `WorkspacePersistenceManager`, use the SQLite adapter;
otherwise fall back to the existing file implementation.

This allows gradual migration: stores can be switched to DB one at a time.
A store that hasn't been migrated yet continues using files.

## Testing

### Conformance test pattern

```python
# tests/unit/persistence/test_inbox_mq_contract.py
@pytest.fixture(params=["file", "sqlite"])
async def inbox_mq(request, tmp_path) -> AsyncIterator[InboxMQ]:
    if request.param == "file":
        yield LocalFileInboxMQ(tmp_path / "inbox")
        return
    conn_mgr = ConnectionManager(tmp_path / "state.db", DatabaseKind.WORKSPACE)
    await conn_mgr.open()
    try:
        yield SqliteInboxMQ(conn_mgr)
    finally:
        await conn_mgr.close()

async def test_receive_and_consume(inbox_mq: InboxMQ):
    # Same test, both backends
    await inbox_mq.receive("session.1", message)
    consumed = await inbox_mq.consume("session.1")
    assert len(consumed) == 1
```

### SQLite-specific tests

- Multi-connection WAL test (framework + CLI writing concurrently)
- Migration idempotency (run twice → no error)
- Crash recovery (kill without close → reopen → data intact)
- Busy timeout (two writers → second waits, succeeds)
- Generated column correctness (scope JSON → derived column values)
- Partial unique index (one-active-turn invariant)

## Operational Notes

### Backup

SQLite backup = copy the `.db` file (after WAL checkpoint). The `PRAGMA
wal_checkpoint(TRUNCATE)` on shutdown ensures the WAL is merged into the
main file, so a cold copy of `state.db` alone is a complete backup.

For hot backup (while bot is running), use SQLite's Online Backup API
(`sqlite3.Connection.backup()`), which copies a consistent snapshot without
blocking writes.

### File size

The `.modex` data audit showed ~50MB total across all file types. The DB
portion will be a fraction of that (structured metadata only; media/overflow
stay as files). SQLite handles databases up to 281 TB; our use case is
trivially small.

### Vacuum

SQLite files can accumulate free pages after deletions. `PRAGMA auto_vacuum`
is not enabled by default. For our data sizes, manual `VACUUM` once a month
(or never) is sufficient. Do not enable `auto_vacuum=FULL` — it doubles
write overhead for no benefit at our scale.
