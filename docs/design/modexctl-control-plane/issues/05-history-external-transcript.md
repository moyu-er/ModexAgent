# 05 — History for external agents (transcript source)

**What to build:** Extend `BotControlFacade.history()` to handle external coding agent sessions (Pi, OpenCode) by selecting the transcript source when `execution_strategy == EXTERNAL_CODING`.

The facade loads the complete transcript event sequence for the exact `session_id` (not prefix fan-in via `load_sessions_by_prefix()`), runs the existing transcript materialization (group by turn id, coalesce text/reasoning by part id, pair tool calls/results by call id), projects materialized blocks to `HistoryMessage` records, omits unavailable fields (no `message_id` for transcript-derived records; no fabricated timestamps, tool_call_ids, or content), discards blocks with no representable CLI history record, orders the resulting logical records newest-first, and applies `limit` to logical records — never to raw events.

`HistorySource.observable_transcript` is returned in the response so the client can distinguish transcript-derived history from MessageStore history without conflating it with provider-native session memory.

**Blocked by:** 04 (native history end-to-end — establishes the facade, models, route, and CLI command that this ticket extends).

**Status:** done (commit 24e85772)

- [x] `BotControlFacade.history()` selects `HistorySource.observable_transcript` when `execution_strategy == EXTERNAL_CODING`.
- [x] Transcript is loaded for the exact `session_id` (no prefix fan-in).
- [x] Complete event sequence is materialized before any limiting.
- [x] Text/reasoning chunks are coalesced by part id into single logical records.
- [x] Tool calls and results are paired by call id.
- [x] `message_id` is absent from transcript-derived `HistoryMessage` records.
- [x] No fields are fabricated (absent fields omitted via `exclude_none`).
- [x] `limit` is applied to logical records, not raw events.
- [x] `HistorySource.observable_transcript` returned in `HistoryResult.source`.
- [x] Facade test (Seam 1): mocked `TranscriptStore` returns sample events; facade returns correct materialized `HistoryResult` with right coalescing, field omission, ordering, and limit.
- [x] CLI test (Seam 2): mock control server returns transcript-derived `HistoryResult`; CLI produces JSONL with correct fields (no `message_id`), exit code 0.
- [x] Empty transcript returns 200 with `items: []`.
