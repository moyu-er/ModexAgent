# 03 — New `history` subcommand (env-gated, JSON Lines output)

**What to build:** From the agent's perspective, invoking `modexctl history --agent <name> --invocation-id <id> [--limit N]` prints the last N messages from that subagent session as JSON Lines — one JSON object per line, newest first. The output is strictly N lines of JSON (or zero + a stderr "No history found" message), with no headers, footers, or separators, so I can pipe it directly to `jq` or parse line-by-line. Each line contains only the 8 core fields from the VO whitelist — internal markers (`_deleted`, `_pinned`, `token_count`, etc.) are stripped. Soft-deleted messages are included (true history).

**Env-gating consensus (per user requirement):** The `history` command registers under the same five `MODEX_COMM_*` env vars as `send`/`agents` (`MODEX_SESSION_ID`, `MODEX_AGENT_NAME`, `MODEX_INBOX_ROOT`, `MODEX_AGENT_POOL_MAP`, `MODEX_TARGETS`), PLUS an additional `MODEX_COMM_KIND=subagent` requirement. No new env vars are introduced. When env is satisfied, `history` appears in `modexctl --help`; when not, it is absent (running `modexctl history` errors as unknown command). This matches the existing pattern where `send`/`agents` are gated by the five comm env vars and the workflow commands are gated by an additional three `MODEX_WORKFLOW_*` vars — `history` adds a single `MODEX_COMM_KIND=subagent` predicate on top of the comm gate.

The command opens a short-lived stdlib `sqlite3` connection to `<workspace>/.modex/state.db` (same cross-process pattern as `SqliteInboxMQ.deliver()`) and queries the `memory_session_messages` table. The `scope_key` is computed as `RecordScope(session_id="{invocation_id}.{agent}").canonical()` — which yields `{"session_id": "<invocation_id>.<agent>"}` because `SessionScope.extract()` only populates `session_id` and `model_dump(exclude_none=True)` drops the unset `pool` (ADR-0028). No MemorySystem assembly, no scope resolver, no pool knowledge required. Row-to-dict reassembly reuses the existing `_assemble_message` helper (owns ColumnProjection round-trip per ADR-0030; reimplementation would risk silent data corruption).

**No FILE backend support.** If `state.db` is missing or the table is empty, print `No history found for session '{session_id}'.` to stderr and exit 0. There is no fallback to reading `messages.jsonl` — that would replicate `DefaultScopedStorage` internals in the CLI.

**Blocked by:** 01 — Disable rich rendering across all modexctl commands.

**Status:** done

- [x] **Env-gating**: when all five `MODEX_COMM_*` env vars are set AND `MODEX_COMM_KIND=subagent` → `history` is registered and visible in `modexctl --help`.
- [x] **Env-gating**: when any of the five `MODEX_COMM_*` env vars is missing/empty → `history` is NOT registered (running `modexctl history` errors as unknown command). Same fail-closed behavior as `send`/`agents`.
- [x] **Env-gating**: when `MODEX_COMM_KIND` is unset or `normal` → `history` is NOT registered, even if all five comm env vars are present. (Peer main agents do not see this command.)
- [x] **Env-gating**: the fail-closed stale-app behavior (env removed/emptied after `build_app()`) applies to `history` just as it does to `send`/`agents` — invocation errors with `EXIT_USAGE` and names the missing env var.
- [x] `--agent` is required (omitting → usage error).
- [x] `--invocation-id` is required (omitting → usage error).
- [x] `--limit` defaults to 3 when omitted.
- [x] `--limit 5` returns up to 5 messages.
- [x] `--limit 100` is silently clamped to 10 (returns up to 10, no error, no warning).
- [x] `--limit 0` → usage error.
- [x] `--limit -1` → usage error.
- [x] Output is JSON Lines: each physical line is one JSON object, `json.loads`-parseable.
- [x] Output has NO header line, NO footer line, NO separator lines — strictly N lines of JSON (or zero lines).
- [x] Output ordering is newest-first (matches SQL `ORDER BY created_at DESC`).
- [x] Each line contains ONLY fields in the VO whitelist: `role`, `content`, `tool_calls`, `tool_call_id`, `tool_name`, `name`, `created_at`, `message_id`. No `_deleted`, `_pinned`, `token_count`, `is_content_json`, `content_format`, `reasoning_content`.
- [x] `content` is preserved verbatim — `str` stays `str`, `list` (multimodal `ContentPart`) stays `list`. No coercion.
- [x] `content` containing a literal `\n` is JSON-escaped to `\\n` so each message stays on one physical line. (Verify by `result.stdout.count("\n") == N` where N is the number of returned messages.)
- [x] `tool_calls` (when present) is a list; each `ToolCall` retains its intrinsic `id` / `type` / `function` (name + arguments) nested fields — these are NOT stripped by the outer whitelist.
- [x] Soft-deleted messages (`state='soft_deleted'` in the table) are included in the output (true history). The `_deleted` marker is stripped by the VO filter, so the agent sees the message content but not the marker.
- [x] When `state.db` does not exist → stderr `No history found for session '{session_id}'.`, exit 0.
- [x] When `state.db` exists but `memory_session_messages` table is empty for the given `scope_key` → stderr `No history found for session '{session_id}'.`, exit 0.
- [x] When the workspace uses FILE backend (no `state.db`) → same "No history found" behavior. The CLI does NOT read `messages.jsonl`.
- [x] The `scope_key` passed to the SQL `WHERE` clause equals `RecordScope(session_id="{invocation_id}.{agent}").canonical()` — verified by cross-referencing with a row written by the bot's `SqliteMessageStore` for the same session.
- [x] The query reuses `_assemble_message` from `modex_agent.persistence.adapters.message_store` for row-to-dict reassembly (no reimplementation of ColumnProjection round-trip logic).

## Comments

### Why this is blocked by 01

The D3 acceptance criteria include assertions about `history --help` output shape (the new command must appear in `--help` without box-drawing noise when env is satisfied). Ticket 01 stabilizes the help rendering first so D3's help assertions have a stable baseline.

### Why this is NOT blocked by 02

T2 and T3 are independent. T3 does not depend on `--invocation-id` being implemented on `send` — `history`'s `--invocation-id` is its own required parameter, unrelated to `send`'s optional `--invocation-id`. They can be implemented in parallel after T1 lands.

### Env-gating design rationale

The user requirement explicitly called out that "指令的支持依赖环境变量, 这个是modexctl指令的共识" (command availability depends on env vars — this is the modexctl consensus). The existing pattern:
- `send` / `agents` → gated by 5 `MODEX_COMM_*` env vars.
- `submit` / `next-steps` / `task` / `workflow` → gated by 5 `MODEX_COMM_*` + 3 `MODEX_WORKFLOW_*` env vars.

`history` follows the same pattern: 5 `MODEX_COMM_*` + 1 `MODEX_COMM_KIND=subagent` predicate. This keeps the gating logic in `build_app()` uniform — each command group is a `if _missing_X_env_key() is None: app.command(...)` block. No new env vars, no new gating mechanism.

The `MODEX_COMM_KIND=subagent` predicate is necessary because `history` is semantically meaningless for peer main agents (peer sessions do not have an `invocation_id` concept — ADR-0019 prefix-reuse produces a session_id the sender has no visibility into). Restricting to `subagent` prevents confusion.

### VO whitelist as a decision-rich prototype snippet

The whitelist is a single frozenset — the decision-rich part is WHICH fields are in/out, not the implementation. From the spec:

```python
_HISTORY_VO_FIELDS = frozenset({
    "role",
    "content",
    "tool_calls",
    "tool_call_id",
    "tool_name",
    "name",
    "created_at",
    "message_id",
})
```

Stripped fields and why:
- `_deleted`, `_pinned` — internal state markers, not part of "core" message identity.
- `token_count` — internal budget tracking, agent does not need it.
- `is_content_json` — internal encoding marker, leaks ColumnProjection internals.
- `content_format` — internal format marker.
- `reasoning_content` — explicitly excluded per user decision (not core history).

`content` is preserved verbatim (`str | list[ContentPart] | None`) — the rare multimodal message round-trips as a list and the agent is expected to handle both shapes. No coercion.

### Reuse of `_assemble_message`

`_assemble_message` is module-private (`_` prefix) in `modex_agent.persistence.adapters.message_store`. It is reused across module boundaries deliberately — it owns the ColumnProjection (ADR-0030) round-trip logic and any divergence would create silent data corruption. If it later moves or becomes public, the import path changes; the call shape does not. This is noted in the spec's "Consequences" section under "Neutral".
