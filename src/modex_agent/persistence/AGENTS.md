<!-- Parent: ../AGENTS.md -->
<!-- Created: 2026-08-15 -->

# persistence

## Purpose

Hybrid persistence layer (ADR-0023, ADR-0028~0031): per-workspace and registry
SQLite databases behind `ConnectionManager` + `MigrationRunner`, a
`PersistenceBackend`-driven IOC switch between the legacy file stores and the
SQLite adapters, and SQLite implementations of the split memory-store ABCs and
runtime-state stores. All timestamps are INTEGER epoch milliseconds (ADR-0029).

## Key Files

| File | Description |
|------|-------------|
| `connection.py` | `ConnectionManager` — per-DB `aiosqlite` lifecycle, `Transaction` context manager, `ConnectionNotOpenError` / `NestedTransactionError`. |
| `migration.py` | `MigrationRunner` + `DatabaseKind` (`WORKSPACE` / `REGISTRY`) + `MigrationFile` — named, ordered SQL migrations loaded from package resources. |
| `config.py` | `PersistenceBackend` enum (`FILE` / `SQLITE`) + `PersistenceConfig` — drives IOC factory selection between file stores and SQLite adapters. |
| `column_projection.py` | `ColumnProjection` / `ColumnCodec` — declarative dict ↔ typed-columns + residual-JSON codec for SQLite adapters (ADR-0030); replaces hand-rolled per-adapter projection. |
| `coordinator.py` | SQLite decision coordinator — atomic `TurnSnapshot` update + `ApprovalAuditEntry` append in one `ConnectionManager.transaction()`; the only place spanning both tables. |
| `memory_registry.py` | Hybrid memory registry — SQLite-backed structured state combined with file documents. |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `adapters/` | 12 SQLite adapters for the split store + runtime-state ABCs: `message_store`, `kv_store`, `cursor_store`, `archive_store`, `session_store`, `turn_state_store`, `todo_store`, `inbox_mq`, `pool_routing_store`, `workspace_registry_store`, `external_session_map_store`, `approval_audit_store`. |
| `session_artifacts/` | Session artifact cleanup (ADR-0018, plan §12): `SessionArtifactCleaner`/`SessionDatabaseCleaner` ABCs, `DefaultSessionArtifactCleaner` (eleven-unit idempotent file deletion + `clean_record_and_transcript` fast path), `SessionCleanupResult` + scope-identity errors, file scope/pool discovery, `SqliteSessionDatabaseCleaner`. |
| `managers/` | Lifecycle managers — `WorkspacePersistenceManager` (opens the workspace DB at materialize, closes at evict; builds DB-backed `MemoryStoreBundle`s with four independent adapters), `RegistryPersistenceManager` (owns the registry DB: `workspaces` + `session_workspace_map`). |
| `migrations/` | Per-DB SQL migrations — `workspace/001_initial.sql`, `registry/001_initial.sql`, executed by `MigrationRunner`. |

## For AI Agents

### Working In This Directory
- All writes go through `ConnectionManager.transaction()`; nested transactions raise.
- Schema changes = new migration files under `migrations/<db>/`; never edit applied migrations.
- Store timestamps as INTEGER ms (`modex_agent.utils.time.now_ms`, ADR-0029).
- Adapters project dict fields via `ColumnProjection` (ADR-0030) — no hand-rolled column extraction.
- `PersistenceBackend` selection happens in IOC factories (config.py), not at adapter call sites.

## Dependencies

### Internal
- `modex_agent.core` — `RecordScope` (scope identity for artifact cleanup)
- `modex_agent.memory`, `modex_agent.runtime`, `modex_agent.workspace` — path derivations for per-session artifact units (legal: persistence sits above them; ADR-0006 polices core only)
- `modex_agent.memory.core.split_stores` — `MessageStore` / `KVStore` / `CursorStore` / `ArchiveStore` ABCs, `MemoryStoreBundle`
- `modex_agent.runtime` — `TurnStateStore` (implemented by `adapters/turn_state_store.py`)

### External
- `aiosqlite` — async SQLite driver (the CLI uses stdlib `sqlite3`)
