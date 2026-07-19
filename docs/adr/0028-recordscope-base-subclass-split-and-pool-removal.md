# RecordScope base/subclass split and pool dimension removal

## Status

Accepted (2026-07-19). Revises the `RecordScope` contract documented in
ADR-0023 D2 and the glossary entry in `CONTEXT.md`.

## Context

`RecordScope` (defined in `modex_agent/core/scope.py`) is the frozen Pydantic
model carrying every configurable isolation dimension across the persistence
layer. ADR-0023 D2 specified it with **12 hard-coded fields** including `pool`,
and made its `canonical()` JSON the sole source for the DB `scope` /
`scope_key` columns and their `STORED` generated columns.

Two problems emerged:

1. **`pool` is a business concept, not a framework dimension.** The framework's
   own `PoolRoutingStore` (in `modex_agent/multi_agent/pool_router.py`) routes
   sessions to pools via a separate `pool_name` business column; it does not
   read `RecordScope.pool`. The only framework code that reads `scope.pool` is
   `SqliteSessionDatabaseCleaner` (`session_cleanup.py:105`), and only as a
   backward-compat fallback for legacy non-canonical scope JSON. All real
   `pool` consumers are in `examples/bot_project/bot/` — the bot's pool
   concept, not the framework's. Meanwhile 13 workspace-DB tables carried a
   `pool TEXT GENERATED ALWAYS AS (json_extract(scope, '$.pool')) STORED`
   column plus an `idx_*_pool` partial index, and **zero SQL `WHERE pool = ?`
   query ever fired** against any of them. The columns and indexes were pure
   dead weight.

2. **The 12-field hard-coded model cannot grow business dimensions without
   framework changes.** A business that wanted, say, a `region` or `org`
   isolation dimension would have to edit the framework `RecordScope`, release
   a new framework version, and update every table's generated-column list.
   This couples business evolution to framework releases — the opposite of the
   framework/examples separation (rule 5 in `rules/architecture.md`).

## Decision

1. **`RecordScope` becomes a framework base class.** It carries only
   framework-level dimensions: `workspace_id, session_id, session_prefix,
   agent_id, agent_role, user_id, tenant_id, channel, chat_id,
   invocation_id, parent_session_id`. The `pool` field is **removed** from the
   base.

2. **Business layers subclass `RecordScope` to add business dimensions.** The
   bot project defines `BotRecordScope(RecordScope)` (in
   `examples/bot_project/bot/scope.py`) re-adding `pool: str | None = None`.
   Other business layers may define their own subclasses with their own
   dimensions.

3. **Canonical JSON divergence is intentional.** A base `RecordScope` and a
   subclass instance with extra non-`None` fields produce **different**
   canonical JSON, therefore **different `scope_key` values**. Records written
   under the base class and the subclass land in separate storage buckets by
   construction — framework-managed records vs business-scoped records are
   naturally isolated. This is a feature, not a bug: it prevents accidental
   cross-bucket reads.

4. **The `pool` generated column and `idx_*_pool` indexes are removed** from
   all 13 workspace-DB tables. The `SqliteSessionDatabaseCleaner`'s
   `if scope.pool is not None:` backward-compat fallback
   (`session_cleanup.py:105`) is deleted — the user explicitly waived data
   migration, so the legacy-format fallback is dead code.

5. **`pool_routing` table keeps its business column `pool_name`** (queried by
   `pool_routing_store.py:48,88`) and drops only the redundant `pool` generated
   column. `idx_routing_pool` is replaced by `idx_routing_pool_name`.

6. **All `RecordScope(pool=...)` constructions in the business layer**
   (8 sites in `examples/bot_project/bot/`) become `BotRecordScope(pool=...)`.
   The 4 `RecordScope(...)` constructions *without* `pool` in `session_gc.py`
   stay on the base class.

## Consequences

- **Adding a business isolation dimension no longer requires framework
  changes.** A business layer subclasses `RecordScope`, adds its field, and
  writes records under that subclass — the new dimension flows through
  `canonical()` into `scope_key` automatically. To get a queryable generated
  column the business layer still adds `ALTER TABLE ... GENERATED ALWAYS AS
  (json_extract(scope_key, '$.new_dim')) STORED` in its own migration, but the
  framework write path is untouched.

- **Mixing base and subclass in the same table partitions records.** This is
  documented as intentional. Callers that want a single bucket must use one
  class consistently.

- **`SqliteSessionDatabaseCleaner` simplifies.** The `pool`-aware
  backward-compat DELETE branch is gone; cleanup is a uniform
  `DELETE FROM {table} WHERE scope_key = ?`.

- **~13 generated columns + ~13 partial indexes removed.** Smaller schema,
  faster writes (no generated-column recompute on `scope_key` change), no
  dead-index maintenance.

- **`examples/bot_project/bot/scope.py` is a new file.** The business layer
  owns its `BotRecordScope` subclass; the framework stays pool-agnostic.

- **`CONTEXT.md` `RecordScope` and `Generated Scope Column` entries updated**
  to reflect the base/subclass split and the "only dimensions with a real
  query path get a generated column" rule.
