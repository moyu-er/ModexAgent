# Tickets: Persistence Schema Optimization (Phase 1)

A one-line summary: Phase 1 refactor unifying timestamps to int ms, merging scope/scope_key, removing the `pool` dimension, dropping dead tables, minimizing `inbox_topics`, simplifying `inbox_messages`, and introducing `ColumnProjection`. Reference spec: `docs/design/persistence-schema-optimization/PRD.md`. Reference ADRs: ADR-0028, ADR-0029, ADR-0030, ADR-0031.

Work the **frontier**: any ticket whose blockers are all done. Three tickets (T1, T2, T3) have no blockers and can start immediately in parallel.

---

## T1: 框架基础设施（utils/time.py + column_projection.py）✅ DONE

**What to build:** The single timestamp producer (`now_ms()` / `now_s()`) migrated from `core/session_id.py` to `modex_agent/utils/time.py` with backward-compat re-export from `core/session_id.py`. The `ColumnProjection` declarative field-mapping abstraction (`ColumnProjection`, `ColumnField`, `ColumnCodec`, `IdentityCodec`, `ContentCodec`) that lets SQLite adapters split a dict into table columns plus a residual JSON column and re-assemble on read. `ContentCodec` handles the `str`-vs-`list[dict]` content duality via an `is_content_json` companion column. Complete unit tests covering `split`/`assemble` round-trip, codec behavior, candidate key priority, and companion-column consistency.

**Blocked by:** None — can start immediately.
**Status:** ✅ DONE (Batch 1) — 96 scoped tests pass; mypy clean; ruff ANN401 only (consistent with existing codebase).

- [x] `modex_agent/utils/time.py` exists with `now_ms() -> int` and `now_s() -> float`
- [x] `core/session_id.py` re-exports `now_ms` for backward compatibility (via `as` idiom for mypy `no_implicit_reexport`)
- [x] `modex_agent/persistence/column_projection.py` exists with `ColumnProjection`, `ColumnField`, `ColumnCodec`, `IdentityCodec`, `ContentCodec`
- [x] `ColumnCodec.encode(column, value)` returns `dict[str, Any]` (allowing fan-out to companion columns like `is_content_json`)
- [x] `ColumnProjection.split(data)` removes all candidate keys from the residual JSON
- [x] `ColumnProjection.assemble(columns, json_str)` re-injects under the first candidate key only
- [x] `ContentCodec` round-trips both `str` and `list[dict]` via `is_content_json` flag
- [x] `tests/unit/persistence/test_column_projection.py` exists and passes (34 tests)
- [x] `tests/unit/utils/test_time.py` verifies `now_ms()` returns int milliseconds (6 tests)

---

## T2: RecordScope 基类化 + BotRecordScope ✅ DONE

**What to build:** The framework's `RecordScope` Pydantic model becomes a base class by removing the `pool` field (now 11 dimensions). The bot project owns a new `BotRecordScope(RecordScope)` subclass that re-adds `pool`. All business-layer constructions of `RecordScope(pool=...)` switch to `BotRecordScope(pool=...)`. A base `RecordScope` and a `BotRecordScope` with non-`None` `pool` produce different canonical JSON — intentional per ADR-0028.

**Blocked by:** None — can start immediately.
**Status:** ✅ DONE (Batch 1) — 10 sites replaced (spec said 8, actual 10); canonical JSON divergence verified; `core/session_scope_discovery.py` + `core/cleanup.py` also updated (necessary — they constructed `RecordScope(pool=...)` which would break with `extra="forbid"`).

- [x] `RecordScope` in `modex_agent/core/scope.py` has no `pool` field
- [x] `examples/bot_project/bot/scope.py` exists with `BotRecordScope(RecordScope)` adding `pool: str | None = None`
- [x] All 10 `RecordScope(pool=...)` sites use `BotRecordScope(pool=...)` (grep confirmed zero residual)
- [x] `job.scope` type annotations in `session_gc.py` accommodate `BotRecordScope`
- [x] `BotRecordScope(pool="x").canonical()` contains `"pool":"x"`; `RecordScope().canonical()` does not
- [x] `core/session_scope_discovery.py` + `core/cleanup.py` updated (necessary consequence)
- [x] Known: `test_inbox_mq.py` collection error (uses `RecordScope(pool=...)` in test setup — to be fixed in T12)

---

## T3: 迁移 SQL 重写 + schema 结构测试 ✅ DONE

**What to build:** The framework's `001_initial.sql` migrations (workspace + registry) are rewritten to define the new schema directly. 15 workspace tables + 2 registry tables with target schema per ADR-0028/0029/0031.

**Blocked by:** None — can start immediately.
**Status:** ✅ DONE (Batch 1) — 96 scoped tests pass (workspace_schema + registry_schema + migration + column_projection + time).

- [x] `persistence/migrations/workspace/001_initial.sql` defines 15 workspace tables with target schema
- [x] `persistence/migrations/registry/001_initial.sql` defines 2 registry tables with target schema
- [x] No `scope` column on any scoped table (only `scope_key`)
- [x] No `pool` generated column on any table
- [x] `inbox_dead_letter` and `workspace_meta` tables do not exist
- [x] `inbox_topics` has only minimal columns (topic_id/owner_scope_key/scope_key/session_id/created_at/updated_at)
- [x] `inbox_messages` has no dead business columns
- [x] All timestamp columns are `INTEGER` with ms-epoch DEFAULT (except `schema_migrations.applied_at`)
- [x] Every mutable table has an `updated_at` trigger
- [x] Append-only tables have no `updated_at` and no trigger
- [x] `test_workspace_schema.py` passes (32 tests)
- [x] `test_registry_schema.py` passes (15 tests)
- [x] `test_migration.py` passes (9 tests)

---

## T4: memory_session_messages adapter + ColumnProjection 试点 [DONE]

**What to build:** `SqliteMessageStore` adopts `_MESSAGE_PROJECTION` (`ColumnProjection` with fields `message_id`, `role`, `content`+`is_content_json` via `ContentCodec`, `token_count`) — on write, the dict is split into column values plus a residual `message_json` that excludes the extracted fields; on read, the dict is re-assembled from columns plus the residual JSON. The `role` column becomes a real indexed column with `CHECK (role IN ('user','assistant','system','tool'))`. The 4 `json_extract(message_json, '$.id') = ? OR json_extract(message_json, '$.message_id') = ?` lookups become simple `message_id = ?` column equality. `memory_revisions` writes drop the `scope` column. `time.time()` → `now_ms()`. The file backend (`DefaultScopedStorage`) is unchanged (stores the full dict wholesale — its `messages.jsonl` naturally contains fields the SQLite backend extracts to columns).

**Blocked by:** T1 (ColumnProjection), T3 (new schema)

- [x] `SqliteMessageStore` defines `_MESSAGE_PROJECTION` with `message_id`, `role`, `content`+`is_content_json`, `token_count`
- [x] `append_message` / `save_messages` use `_MESSAGE_PROJECTION.split(message)` for column extraction
- [x] `load_messages` / `load_all_messages` use `_MESSAGE_PROJECTION.assemble(columns, json_str)` for dict reassembly
- [x] `pin_message` / `unpin_message` / `delete_message` query `WHERE message_id = ?` (no `json_extract`)
- [x] `memory_revisions` INSERT/UPDATE omit the `scope` column (only `scope_key`)
- [x] No `time.time()` calls in `message_store.py` — all use `now_ms()`
- [x] `tests/conformance/test_message_store_conformance.py` passes (round-trip: append → load returns semantically equal dict)
- [x] `tests/conformance/test_message_row_state_conformance.py` passes (state machine: normal → pinned → soft_deleted)

---

## T5: inbox 表簇 adapter 整改 [DONE]

**What to build:** `SqliteInboxMQ` adopts `_INBOX_PROJECTION` (`message_id`, `message_type`, `session_id` extracted to columns; `payload_json` stores the full `InboxMessage` dict including `source`/`content`/`metadata`/envelope fields). The 5 dead business columns (`source_name`, `source_kind`, `content`, `envelope_session_id`, `envelope_agent_session_id`) are no longer written or read — they were `SELECT`ed only to reconstruct `InboxMessage`, a role `payload_json` now fulfills. The 5 `UPDATE inbox_topics SET state=.../last_active=.../message_count=...` calls are deleted — `inbox_topics` is now insert-once-delete-on-cleanup. `inbox_delivered_ids` INSERT drops `session_id`; FK simplifies from composite `(owner_scope_key, scope_key, session_id)` to single-column `scope_key` → `inbox_topics(scope_key)`. The sync `deliver()` path (cross-process CLI) is updated identically. `_row_to_message` uses `_INBOX_PROJECTION.assemble()`. `InboxMessage.timestamp` stays `datetime` in the ABC contract — the adapter converts `int(message.timestamp.timestamp() * 1000)` on write and `datetime.fromtimestamp(row["created_at"] / 1000, tz=UTC)` on read.

**Blocked by:** T1 (ColumnProjection), T3 (new schema)

- [x] `SqliteInboxMQ` defines `_INBOX_PROJECTION` with `message_id`, `message_type`, `session_id`
- [x] `receive` / `deliver` INSERT into `inbox_messages` with only extracted columns + `payload_json`
- [x] `_row_to_message` uses `_INBOX_PROJECTION.assemble()` (no manual column-by-column reconstruction)
- [x] 5 `UPDATE inbox_topics SET state/last_active/message_count` calls deleted
- [x] `inbox_topics` INSERT writes only `owner_scope_key`, `scope_key`, `session_id` (timestamps via DEFAULT)
- [x] `inbox_delivered_ids` INSERT omits `session_id`
- [x] `InboxMessage.timestamp` ↔ `created_at` int ms conversion at adapter boundary
- [x] No `time.time()` calls — all use `now_ms()`
- [x] `tests/conformance/test_inbox_mq_conformance.py` passes (receive → consume round-trip; exactly-once dedup; FIFO ordering)

---

## T6: 其他 memory 表 adapter 整改（kv/cursors/revisions/archive） [DONE]

**What to build:** Mechanical adapter整改 for 4 SQLite adapters: `SqliteKVStore`, `SqliteCursorStore`, `SqliteArchiveStore`, and the `memory_revisions` write path inside `SqliteMessageStore`. All: `scope` column references → `scope_key`; `time.time()` → `now_ms()`. Tables missing `created_at` (`memory_kv`, `memory_cursors`, `memory_revisions`, `memory_archive_state`) get it via the new schema (DEFAULT + trigger). `memory_archive_entries` is append-only (no `updated_at`, no trigger). `memory_archive_state` `pool` generated column removed.

**Blocked by:** T1 (now_ms), T3 (new schema)

- [x] `SqliteKVStore`: `scope` → `scope_key`, `time.time()` → `now_ms()`
- [x] `SqliteCursorStore`: `scope` → `scope_key`, `time.time()` → `now_ms()`
- [x] `SqliteArchiveStore` (entries + state): `scope` → `scope_key`, `time.time()` → `now_ms()`
- [x] `memory_revisions` write path: `scope` column omitted (only `scope_key`)
- [x] `tests/conformance/test_kv_store_conformance.py` passes
- [x] `tests/conformance/test_cursor_store_conformance.py` passes
- [x] `tests/conformance/test_archive_store_conformance.py` passes

---

## T7: turn_snapshots + sessions adapter 整改 [DONE]

**What to build:** `SqliteTurnStateStore` and `SqliteSessionStore` updated to the new schema. Both: `scope` → `scope_key`; `time.time()` → `now_ms()`. `SqliteTurnStateStore` fixes the unit-mismatch bug (`created_at` was int ms, `updated_at` was float seconds in the same row) — both now int ms. Phase 1 adapter-boundary conversion for `TurnSnapshot.created_at` (which stays `float` seconds in the runtime dataclass until Phase 2): on `save_turn`, write `int(snapshot.created_at * 1000)`; on `_decode`, re-inject `row["created_at"] / 1000.0` into the payload before codec decode; on `list_active_turns` with `scope.created_before`, convert `int(scope.created_before * 1000)` for the SQL parameter. `SqliteSessionStore` `_SESSION_COLUMNS` still reads `agent_id`/`parent_session_id` from generated columns (unchanged); timestamp writes use `now_ms()` or rely on DEFAULT.

**Blocked by:** T1 (now_ms), T3 (new schema)

- [x] `SqliteTurnStateStore`: `scope` → `scope_key` in all SQL
- [x] `SqliteTurnStateStore`: `now = time.time()` → `now = now_ms()`
- [x] `SqliteTurnStateStore.save_turn`: `created_at` written as `int(snapshot.created_at * 1000)` (Phase 1 boundary conversion)
- [x] `SqliteTurnStateStore._decode`: `snapshot_payload["created_at"] = row["created_at"] / 1000.0` before codec decode
- [x] `SqliteTurnStateStore.list_active_turns`: `scope.created_before` converted to `int(scope.created_before * 1000)` for SQL
- [x] `SqliteSessionStore`: `scope` → `scope_key` in INSERT/SELECT
- [x] `SqliteSessionStore`: timestamp writes use `now_ms()` or DEFAULT
- [x] No `time.time()` calls in either adapter
- [x] `tests/conformance/test_turn_state_store_conformance.py` passes (save → load round-trip; Phase 1 boundary conversion transparent)
- [x] `tests/conformance/test_session_store_conformance.py` passes

---

## T8: todos + approval_audit_log + external_session_map + pool_routing adapter 整改 [DONE]

**What to build:** Mechanical adapter整改 for 4 SQLite adapters. `SqliteTodoStore`: `scope` → `scope_key`, `time.time()` → `now_ms()`, `updated_at` via DEFAULT+trigger. `SqliteApprovalAuditStore`: `scope` → `scope_key`, `decided_at` via `now_ms()`, append-only (no `updated_at`). `SqliteExternalSessionMapStore`: `scope` → `scope_key`, `last_committed_at` via `now_ms()`, `invalidated` CHECK (0/1). `SqlitePoolRoutingStore`: `scope` → `scope_key`, `SELECT pool_name, pool` → `SELECT pool_name` (drop dead generated column), `idx_routing_pool_name` replaces `idx_routing_pool`, timestamps via `now_ms()` or DEFAULT.

**Blocked by:** T1 (now_ms), T3 (new schema)

- [x] `SqliteTodoStore`: `scope` → `scope_key`, `time.time()` → `now_ms()`
- [x] `SqliteApprovalAuditStore`: `scope` → `scope_key`, `decided_at` via `now_ms()`
- [x] `SqliteExternalSessionMapStore`: `scope` → `scope_key`, `last_committed_at` via `now_ms()`
- [x] `SqlitePoolRoutingStore`: `scope` → `scope_key`, `SELECT pool_name, pool` → `SELECT pool_name`
- [x] No `time.time()` calls in any of the 4 adapters
- [x] `tests/conformance/test_todo_store_conformance.py` passes
- [x] `tests/conformance/test_approval_audit_store_conformance.py` passes
- [x] `tests/conformance/test_external_session_map_store_conformance.py` passes
- [x] `tests/conformance/test_pool_routing_store_conformance.py` passes

---

## T9: 文件后端时间戳统一 [DONE]

**What to build:** File-backend JSON payloads switch to `int` ms timestamps to match SQLite column types, so a `FILE`↔`SQLITE` backend switch produces semantically identical data. `StorageRevision.updated_at` changes from `datetime` to `int` ms (model + 4 file-backend implementations: `scoped_file.py`, `file.py`, `scoped_in_memory.py`, `dir_archive.py`). `ArchiveEntry.created_at` changes to `int` ms; consumers (`memory/layers/archive.py`, `memory/default_system.py`) drop `.isoformat()` calls. `LocalFileInboxMQ` serializes `InboxMessage.timestamp` as `int(message.timestamp.timestamp() * 1000)` instead of `.isoformat()`, and deserializes via `datetime.fromtimestamp(v / 1000, tz=UTC)` — `InboxMessage.timestamp` stays `datetime` in the ABC contract. `ChatMessage.created_at` stays ISO-8601 string (display-only business data, out of scope per ADR-0029).

**Blocked by:** T1 (now_ms)

- [x] `StorageRevision.updated_at` type is `int` (was `datetime`)
- [x] `ArchiveEntry.created_at` type is `int` (was `datetime` or ISO string)
- [x] `scoped_file.py`: `self._updated_at = now_ms()` (3 sites); archive entry `created_at` uses `now_ms()` (2 sites)
- [x] `file.py`: `updated_at=now_ms()`; `now = now_ms()`
- [x] `scoped_in_memory.py`: same as `scoped_file.py` (2 + 2 sites)
- [x] `dir_archive.py`: `updated_at=now_ms()`; archive entry `created_at` uses `now_ms()`
- [x] `memory/layers/archive.py`: `.isoformat()` calls on `created_at` removed (2 sites)
- [x] `memory/default_system.py`: `.isoformat()` call on `created_at` removed
- [x] `server_local.py`: `message.timestamp.isoformat()` → `int(message.timestamp.timestamp() * 1000)` (2 sites)
- [x] `server_local.py`: `datetime.fromisoformat(data["timestamp"])` → `datetime.fromtimestamp(data["timestamp"] / 1000, tz=UTC)` (2 sites)
- [x] `InboxMessage.timestamp` ABC contract stays `datetime`
- [x] `ChatMessage.created_at` stays ISO-8601 string (unchanged)
- [x] Conformance tests pass with file backend (assertions updated for int ms timestamps)

---

## T10: workspaces + session_workspace_map adapter + WorkspaceRecord 模型 [DONE]

**What to build:** `WorkspaceRecord.created_at` and `.last_active` change from `str` (ISO-8601) to `int` (ms epoch) — the model now matches the `workspaces` table column type. `workspace/registry.py` and `workspace/store.py` replace `_now_iso()` helpers with `now_ms()` calls. `SqliteWorkspaceRegistryStore` adapts to int-ms timestamps, `metadata_json NOT NULL DEFAULT '{}'`, and `is_home` CHECK constraint. `session_workspace_map` gets `created_at`/`updated_at` (via DEFAULT + trigger); its adapter INSERT simplifies to just `session_prefix` + `workspace_id` (timestamps via DEFAULT).

**Blocked by:** T1 (now_ms), T3 (new schema)

- [x] `WorkspaceRecord.created_at` and `.last_active` are `int` (ms epoch)
- [x] `workspace/registry.py` and `workspace/store.py` use `now_ms()` (no `_now_iso()`)
- [x] `SqliteWorkspaceRegistryStore` writes int-ms timestamps
- [x] `SqliteWorkspaceRegistryStore` handles `metadata_json NOT NULL DEFAULT '{}'`
- [x] `session_workspace_map` INSERT writes only `session_prefix` + `workspace_id` (timestamps via DEFAULT)
- [x] No `time.time()` calls in `workspace_registry_store.py`
- [x] `tests/conformance/test_workspace_registry_store_conformance.py` passes (assertions updated for int ms)

---

## T11: SqliteSessionDatabaseCleaner 整改 [DONE]

**What to build:** The cleaner's hardcoded 16-table UNION and per-table DELETE statements are updated for the merged `scope_key` column and the dropped `inbox_dead_letter` table. `_SCOPE_DISCOVERY_SQL` changes `SELECT scope FROM {table}` → `SELECT scope_key FROM {table}`. `_SCOPE_DELETES` changes `WHERE scope = ?` → `WHERE scope_key = ?`. The `if scope.pool is not None:` backward-compat fallback (legacy non-canonical scope JSON) is deleted — per the user's directive, no data migration, so the fallback is dead code. `_INBOX_CHILD_TABLES` removes `"inbox_dead_letter"`. All `time.time()` → `now_ms()`.

**Blocked by:** T4, T5, T6, T7, T8, T10 (all adapter tables must be整改d first so the cleaner's SQL matches every table's actual columns)

- [x] `_SCOPE_DISCOVERY_SQL` uses `SELECT scope_key FROM {table}` (no `SELECT scope`)
- [x] `_SCOPE_DELETES` uses `WHERE scope_key = ?` (no `WHERE scope = ?`)
- [x] `if scope.pool is not None:` fallback deleted
- [x] `_INBOX_CHILD_TABLES` does not contain `"inbox_dead_letter"`
- [x] No `time.time()` calls — all use `now_ms()`
- [x] `tests/unit/persistence/test_session_cleanup.py` passes (cascade delete across all 14 workspace tables; `inbox_dead_letter` cascade test removed; `scope` → `scope_key` in all assertions)

---

## T12: 测试套件整改 + 架构 guard [DONE]

**What to build:** The remaining test files that were not updated by adapter tickets (T4-T10) are整改d to match the new schema. `tests/conformance/test_sqlite_specific.py` is substantially rewritten: smoke-test target changes from `workspace_meta` to `sessions`; `pool` generated-column checks removed; timestamp column type checks change from `REAL`/`TEXT` to `INTEGER`; trigger existence checks added (one per mutable table); `scope`/`scope_key` dual-column checks replaced with single `scope_key` checks; `inbox_dead_letter`/`workspace_meta` existence checks replaced with absence assertions. Architecture guard tests in `tests/architecture/` get new assertions: no production code references `workspace_meta` or `inbox_dead_letter`; `RecordScope` does not have a `pool` field; `utils/time.py` exports `now_ms`.

**Blocked by:** T4, T5, T6, T7, T8, T9, T10, T11 (all adapter + cleaner整改 complete so test assertions match the final state)

- [x] `tests/conformance/test_sqlite_specific.py` passes against the new schema
- [x] Smoke-test target is `sessions` (not `workspace_meta`)
- [x] No `pool` generated-column checks
- [x] Timestamp type checks assert `INTEGER`
- [x] Trigger existence checks for every mutable table
- [x] `inbox_dead_letter` and `workspace_meta` absence assertions
- [x] `tests/architecture/` has new guard: no `workspace_meta`/`inbox_dead_letter` references in production code
- [x] `tests/architecture/` has new guard: `RecordScope` has no `pool` field
- [x] `tests/architecture/` has new guard: `utils/time.py` exports `now_ms`
- [x] All architecture guard tests pass

---

## T13: 端到端验证 + bot 项目兼容性 [DONE]

**What to build:** Full end-to-end verification that the Phase 1 refactor preserves behavior across both persistence backends and the bot project. Run the complete conformance suite (14 files, both FILE and SQLITE backends) — all must pass. Run bot project integration tests (workspace initialization, message send/receive, inbox message passing, approval flow, turn suspend/resume, session GC) — all must pass. Verify `bot_webui_transcript_events` (the golden-standard table) is untouched and its adapter (`sqlite_transcript_store.py`) still works. Verify `PersistenceConfig.backend` switching (FILE → SQLITE, SQLITE → FILE) produces semantically equivalent data.

**Blocked by:** T1, T2, T3, T4, T5, T6, T7, T8, T9, T10, T11, T12 (everything)

- [x] `pytest tests/conformance/ -v` — all 14 conformance test files pass (both backends)
- [x] `pytest tests/unit/persistence/ -v` — all persistence unit tests pass
- [x] `pytest tests/architecture/ -v` — all architecture guard tests pass
- [x] `pytest examples/bot_project/tests/integration/ -v -m integration` — bot integration tests pass
- [x] `bot_webui_transcript_events` table untouched; `sqlite_transcript_store.py` unchanged
- [x] `PersistenceConfig.backend = FILE` then `SQLITE` produces semantically equivalent data for the same ABC operations
- [x] No `time.time()` calls remain in `src/modex_agent/persistence/adapters/`
- [x] No `scope` column references remain in `src/modex_agent/persistence/` (only `scope_key`)
- [x] No `RecordScope(pool=...)` constructions remain in `examples/bot_project/bot/` (all use `BotRecordScope`)
- [x] No `workspace_meta` or `inbox_dead_letter` references remain in production code

## Post-Implementation Notes (2026-07-19)

All tickets T1-T13 completed. Additional fixes during code review:

- **C1 (Critical):** modexctl _PoolScopedRecordScope vs bot BotRecordScope
  produced different scope_keys (class-name-based stamp). Fixed with
  content-based stamp (sorted extra field names) so structurally identical
  subclasses produce identical canonical JSON.
- **C2 (Critical):** coordinator.py missed ADR-0029 migration — used
  	ime.time() (float seconds) for INTEGER ms columns. Fixed to use
  
ow_ms() and int(created_at * 1000).
- **C3 (Critical):** SqliteSessionStore.pool_resolver was dead code
  (model_copy on extra="forbid" model silently dropped pool). Removed;
  delete_session_rows uses session_id (UNIQUE) not scope_key.
- **I2 (Important):** rchive_store._row_to_entry used loat(row[2])
  but _parse_created_at only accepted int. Fixed to int(row[2]).
- **created_at fidelity:** _assemble_message now projects created_at
  column back into dict (was dropped, causing pruned files to use cleanup
  time instead of message creation time).
- **__init_subclass__ timing:** Uses __annotations__ not model_fields
  (Pydantic v2 populates model_fields after __init_subclass__).
- **packaging:** pyproject.toml now includes ot/**/*.sql (migrations
  directory was not installed in editable installs).
- **tool_call_cleanup plugin:** Removed (unused, broke test collection).

Final test count: 6006 passed, 1 pre-existing failure (architecture guard
test_no_execution_strategy_branches — unrelated to Phase 1, user confirmed
not to fix).