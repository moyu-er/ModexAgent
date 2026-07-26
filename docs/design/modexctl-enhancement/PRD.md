# modexctl Enhancement — PRD

Feature: modexctl agent self-governance enhancement
ADR: ADR-0035
Status: proposed (2026-07-26)

## Problem

`modexctl` is the agent-self-governance CLI introduced by ADR-0022. It
lets external coding agents (Pi, OpenCode) participate in the
ModexAgent multi-agent topology by writing into target pools' inboxes.
Its current surface — `send` and `agents` — is intentionally minimal.

Three gaps were identified during a grilling session against the
existing `send_to_agent` tool surface and the SQLite persistence layer:

1. **Output noise.** Typer's default rich rendering paints box-drawing
   borders around `--help` and exception traces, inflating token count
   and breaking naive line-based parsing for agents reading `2>&1`.
2. **Subagent dispatch is asymmetric with the framework tool.**
   `send_to_agent` accepts `invocation_id` (new vs. resume) and returns
   `AgentSendResult` with `invocation_id` / `session_id` /
   `created_new_task` / `output_path` / `trace_dir`. The CLI accepts
   neither the parameter nor the response surface — it always mints a
   fresh invocation_id, hardcodes `REACT` strategy, and emits a single
   fixed string.
3. **No history inspection.** Agents cannot inspect a subagent's recent
   message history to decide whether to wait, follow up, or abandon a
   stalled task. The data exists in SQLite but is unreachable from the
   CLI.

A fourth capability — cancelling a dispatched subagent task — was
considered but deferred to future work (see ADR-0035 D4).

## Goals

1. Disable rich rendering across all modexctl commands.
2. `send` gains `--invocation-id`, response surface, and
   quadrant-differentiated output.
3. New `history` subcommand for inspecting subagent message history
   (SQLite-only, JSON Lines output, VO-filtered).
4. Record cancellation requirement + background for future design.

## Non-goals

- FILE backend support for `history` (SQLite-only).
- Cross-workspace history reads (subagent-only).
- Cancellation implementation (deferred; see ADR-0035 D4).
- Modifying the `ControlChannel` ABC.
- Modifying the `send_to_agent` tool surface (the framework tool's
  `SubagentDispatchStrategy` does not validate `parent_session_id`
  against the caller — this is a known framework gap, not in scope).

## Decisions (summary — full rationale in ADR-0035)

### D1 — Disable rich rendering

```python
app = typer.Typer(
    name="modexctl",
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
```

Applies uniformly to every command. ANSI color highlights remain
(harmless to agents, useful for human debugging); structural noise
(box-drawing, multi-line tables, markdown rendering in help) is removed.

### D2 — `send` enhancement

#### D2.1 — `--invocation-id` parameter (subagent dispatch only)

| Value | Behavior |
|---|---|
| omitted / `None` / empty | mint fresh 8-byte hex uuid; new subagent session |
| non-empty `foo123` | attempt resume of `foo123.<agent_name>` |

**Session existence check** (subagent path only): one SQL query against
`state.db`:

```sql
SELECT 1 FROM sessions WHERE session_id = ?
```

- Exists → `status: resumed`
- Not exists → mint **fresh** uuid (not `foo123`), `status: new_task
  (provided 'foo123' not found, created new)`
- `parent_session_id` mismatch → treated identically to "not exists"
  (not deepened; cannot legitimately occur in single-main-per-pool
  topology)

**Race accepted**: agent may invoke `send --invocation-id X` again
before the bot has registered session X from the previous send.
Documented contract: "only pass `--invocation-id` for sessions you have
received a `<replied>` for."

Only meaningful when `MODEX_COMM_KIND=subagent`. Ignored for `normal`
and for subagent → parent reply.

#### D2.2 — Quadrant-differentiated output

| # | Quadrant | Output |
|---|---|---|
| ① | `normal` (peer, NATIVE/EXTERNAL) | `Message delivered to '{to}'.\nPeer will process asynchronously. No wait needed.` (no session_id, no invocation_id) |
| ② | `subagent` dispatch, NATIVE | invocation_id + session_id + status + output_path + trace_dir + "wait for <replied>" |
| ③ | `subagent` dispatch, EXTERNAL | invocation_id + session_id + status + "wait for <replied>" (no output_path/trace_dir) |
| ④ | subagent → parent reply | `Reply delivered to parent (session: {parent_sid}).\nParent will continue its turn.` |

**Output contract**: no statement about *how the recipient will reply*
(no "the peer will use `modexctl send`"). The agent learns only that
the message was delivered and what to do next.

**Not-found status** (② and ③ with `--invocation-id` passed but session
not found): `status: new_task (provided '{provided_id}' not found,
created new)`, and `invocation_id:` shows the newly minted uuid.

### D3 — `history` subcommand

#### Surface

```
modexctl history --agent <name> --invocation-id <id> [--limit N]
```

- `--agent` (required)
- `--invocation-id` (required)
- `--limit` (default 3, max 10; >10 clamped, ≤0 rejected)

**Only registered when `MODEX_COMM_KIND=subagent`.** Peer main agents'
history is out-of-scope.

#### Query

Short-lived `sqlite3` connection to `<workspace>/.modex/state.db`:

```sql
SELECT message_id, role, content, is_content_json, token_count,
       message_json, created_at, state
FROM memory_session_messages
WHERE scope_key = ?
ORDER BY created_at DESC
LIMIT ?;
```

- `scope_key = RecordScope(session_id="{invocation_id}.{agent}").canonical()`
  — yields `{"session_id": "<invocation_id>.<agent>"}`. No
  MemorySystem assembly, no pool knowledge required.
- `ORDER BY created_at DESC` (int ms, ADR-0029). `seq` is NOT used —
  its semantics differ between backends.
- **No `state` filter** — soft-deleted messages included ("true
  history").
- **No FILE backend support.** If `state.db` missing or table empty →
  print `No history found for session '{session_id}'.` to stderr, exit
  0.
- **Reuse `_assemble_message`** from
  `modex_agent.persistence.adapters.message_store` — owns the
  ColumnProjection round-trip; reimplementation would risk silent data
  corruption.

#### VO (View Object) field whitelist

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

Stripped: `_deleted`, `_pinned`, `token_count`, `is_content_json`,
`content_format`, `reasoning_content`. `content` is preserved verbatim
(`str | list[ContentPart] | None`).

#### Output: JSON Lines

Each message → one JSON object per line, `json.dumps(msg,
ensure_ascii=False)` (escapes embedded `\n` → `\\n` so each message
stays on one physical line).

Ordering: **newest first** (matches SQL `ORDER BY created_at DESC`).

No header, no footer, no separator. Strictly N lines of JSON (or zero +
stderr "No history found"). Directly pipeable to `jq` or any
line-oriented parser.

### D4 — Cancellation: deferred (Future Work)

Requirement and background recorded in ADR-0035 D4. Summary:

- **Requirement**: `modexctl cancel --agent <name> --invocation-id <id>`
  reusing the existing `CANCEL_TURN` control channel mechanism. CLI
  response must warn the caller that a cancellation notification will
  arrive via the normal `<replied>` channel (via
  `SubagentAutoSendHook`), so the agent does not misinterpret the
  acknowledgement.
- **Why hard**: `InMemoryControlChannel` is process-internal with no
  cross-process `deliver()` analog to `InboxMQ.deliver()` (ADR-0022).
  Three pieces of infrastructure needed: cross-process write surface
  (mirror inbox pattern OR reuse inbox path with a special envelope),
  ReAct-loop cancel checkpoint semantics, and `SubagentAutoSendHook`
  integration.
- **Why deferred**: each piece is non-trivial (comparable to ADR-0022's
  inbox deliver path); ROI is low (agents have alternatives — wait for
  `max_iterations`, follow-up message causing natural timeout, abandon
  session); original requirement explicitly allowed deferral.

## Acceptance criteria

### D1

- [ ] `modexctl --help` output contains no box-drawing characters
      (`─│┌┐└┘├┤┬┴┼`).
- [ ] `modexctl send --invalid-arg` exception trace is plain text (no
      ANSI color codes around the traceback structure, no rich panels).
- [ ] Existing `modexctl send` and `modexctl agents` commands still
      function with identical stdout (modulo the new D2.2 output for
      `send`).

### D2

- [ ] `modexctl send --to X --content Y` (no `--invocation-id`,
      `MODEX_COMM_KIND=subagent`) mints a new uuid, dispatches, prints
      quadrant ② or ③ output with `status: new_task`.
- [ ] `modexctl send --to X --content Y --invocation-id foo123`
      (session `foo123.X` exists in `sessions` table) prints
      `status: resumed`, `invocation_id: foo123`.
- [ ] `modexctl send --to X --content Y --invocation-id foo123` (session
      `foo123.X` does NOT exist) mints a new uuid, prints
      `status: new_task (provided 'foo123' not found, created new)`,
      and `invocation_id:` shows the new uuid (not `foo123`).
- [ ] `modexctl send --to X --content Y` (`MODEX_COMM_KIND=normal`)
      prints quadrant ① output (no `session_id`, no `invocation_id`).
- [ ] `modexctl send --to X --content Y` (subagent → parent reply path)
      prints quadrant ④ output.
- [ ] No output template mentions "the peer will use `modexctl send`" or
      any other statement about the recipient's reply mechanism.
- [ ] `--invocation-id ""` behaves identically to omitting the flag
      (mints new uuid).
- [ ] `--invocation-id` is silently ignored when `MODEX_COMM_KIND=normal`
      (no error; the parameter has no effect on the peer path).

### D3

- [ ] `modexctl history --agent researcher --invocation-id abc12345`
      prints up to 3 JSON Lines (one message per line), newest first.
- [ ] Each line is valid JSON parseable by `json.loads`.
- [ ] Each line contains only fields in `_HISTORY_VO_FIELDS` — no
      `_deleted`, `_pinned`, `token_count`, `is_content_json`,
      `content_format`, `reasoning_content`.
- [ ] `--limit 5` returns up to 5 messages. `--limit 100` is clamped to
      10 (returns up to 10). `--limit 0` or `--limit -1` is a usage
      error.
- [ ] Soft-deleted messages (`state='soft_deleted'`) are included.
- [ ] When `state.db` does not exist or the table is empty, prints
      `No history found for session '{session_id}'.` to stderr and
      exits 0.
- [ ] When `MODEX_COMM_KIND != subagent`, the `history` subcommand is
      not registered (running `modexctl history` errors as unknown
      command).
- [ ] `content` containing literal `\n` is serialized with `\\n` (the
      line stays one physical line).

### D4

- [ ] ADR-0035 D4 contains the full requirement and background.
- [ ] No `cancel` subcommand is registered.

## Implementation notes

### Files to modify

- `src/modexctl/main.py` — Typer app construction (D1), `send` command
  (D2), new `history` command (D3).
- `src/modexctl/__init__.py` — re-exports if any new public surface.
- New module(s) under `src/modexctl/` for the history query + VO filter
  (keep `main.py` focused on CLI plumbing).

### Tests

- Unit: VO filter, scope_key construction, quadrant output formatting,
  limit clamping.
- Integration: `modexctl send` + `modexctl history` against a temporary
  `state.db` fixture.
- Regression: existing `modexctl send` tests still pass (modulo output
  format change).

### Dependencies

- Reuses `modex_agent.persistence.adapters.message_store._assemble_message`
  (private helper; stable).
- Reuses `modex_agent.core.scope.RecordScope` for `canonical()`.
- No new third-party dependencies.

## Open questions

None at decision time. D4 (cancellation) is the only known follow-up.
