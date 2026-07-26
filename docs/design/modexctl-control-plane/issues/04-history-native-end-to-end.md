# 04 — History for native agents end-to-end

**What to build:** A complete vertical slice for `modexctl history` against native ReAct agent sessions, from CLI command through HTTP endpoint to `MessageStore` query and JSONL output.

The `BotControlFacade.history()` method accepts a `HistoryRequest` (containing `caller: AgentSessionRef` with `workspace`, `pool`, `session_id`, `agent_name`, plus `limit`). It resolves the workspace via the shared request resolver (ticket 02), validates the session against `SessionIndexStore`, resolves the pool from `PoolStore`'s agent-to-pool map, reads `execution_strategy` from the pool spec, and — for native strategies — constructs `BotRecordScope(workspace_id, pool, session_id)` and calls `MessageStore.load_all_messages()` (including soft-deleted records).

The result is projected to `HistoryMessage` (eight-field Server Projection with `exclude_none=True`), ordered newest-first, and limited to the validated `limit` (1-10, enforced by Pydantic). Empty history returns 200 with `items: []`.

The `POST /api/control/history` route is a thin adapter: parse JSON → Pydantic → call facade → serialize. The CLI constructs `session_id` from `--invocation-id` and `--agent` arguments, resolves `pool` from `MODEX_AGENT_POOL_MAP`, clamps `limit` >10 to 10, rejects ≤0 as exit 1, and applies its own independent eight-field Client Output Projection before JSONL serialization.

**Blocked by:** 01 (control origin injection), 02 (workspace resolver extraction).

**Status:** done (commit ae6eff06)

- [x] `AgentSessionRef`, `HistoryRequest`, `HistoryResult`, `HistoryMessage`, `HistorySource`, `ControlError` Pydantic models exist in `bot/control/models.py`.
- [x] `BotControlFacade.history()` resolves workspace, validates session, resolves pool, selects native `MessageStore` source, queries with `BotRecordScope`, and returns `HistoryResult`.
- [x] `POST /api/control/history` route registered in the aiohttp server, delegates to facade.
- [x] Route returns `ControlError` JSON for 400/404/409/422/500 cases.
- [x] `limit` enforced as `ge=1, le=10` by Pydantic at the bot boundary.
- [x] CLI `history` command constructs `session_id` as `{invocation_id}.{agent}` and sends `HistoryRequest`.
- [x] CLI clamps `limit` >10 to 10; rejects ≤0 as usage error (exit 1).
- [x] CLI applies independent Client Output Projection (eight-field allowlist) before JSONL output.
- [x] Empty history returns 200 with `items: []`; CLI outputs no JSONL lines, exits 0.
- [x] Soft-deleted messages are included in the result.
- [x] `HistorySource.message_store` is returned in the response.
- [x] `BotControlFacade` test (Seam 1): mocked `MessageStore` returns sample records; facade returns correct `HistoryResult` with right fields, ordering, and limit.
- [x] CLI test (Seam 2): mock control server returns `HistoryResult`; CLI produces correct JSONL output, exit code 0.
- [x] Route test (Seam 3): malformed JSON → 400; missing session → 404; agent_name mismatch → 409; `limit=0` → 400 from Pydantic.
