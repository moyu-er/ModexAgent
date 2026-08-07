> **Superseded.** Content merged into ADR-0035 (modexctl Control Plane),
> `docs/adr/0035-modexctl-control-plane.md`. This archived copy retains the
> original direct-SQLite CLI design for historical traceability. The
> bot-owned HTTP control plane described in the new ADR-0035 supersedes
> this approach.
# modexctl Agent Self-Governance Enhancement

Status: superseded by ADR-0036 (2026-07-26)

> The CLI architecture decisions in this ADR 鈥?direct SQLite inbox delivery,
> env-gated quadrant routing, and local history queries 鈥?are superseded by
> ADR-0036 (modexctl Control Plane), which replaces the legacy CLI with a
> bot-owned HTTP client. The source remains as a reference implementation
> (ADR-0036 D2). This ADR is retained for historical context only.

## Context

`modexctl` (ADR-0022) is the stateless, env-gated CLI that lets external
coding agents (Pi, OpenCode) participate in the ModexAgent multi-agent
topology by writing messages into target pools' inboxes via the synchronous
`SqliteInboxMQ.deliver()` path. Its current surface is intentionally minimal:

```
modexctl send --to <name> [--content <text> | --content-file <path> | --stdin]
modexctl agents
```

The CLI is invoked from agent-driven bash, so its outputs must be
**machine-parseable, low-noise, and structurally predictable**. Three gaps
were identified during a grilling session against the existing
`send_to_agent` tool surface and the SQLite persistence layer:

1. **Output noise.** Typer's default rich rendering paints box-drawing
   borders and ANSI color codes around `--help` and exception traces. The
   command stdout itself is plain text, but the help / error paths still
   emit characters that an agent parsing `2>&1` must filter out.
2. **Subagent dispatch is asymmetric with the framework tool.**
   `send_to_agent` already accepts an `invocation_id` parameter that
   governs new-task vs. resume semantics, and returns
   `AgentSendResult` with `invocation_id`, `session_id`,
   `created_new_task`, `output_path`, and `trace_dir`. The CLI accepts
   neither the parameter nor the response surface: it always mints a fresh
   invocation_id for same-pool dispatch, hardcodes `REACT` strategy, and
   emits a single fixed string regardless of dispatch outcome.
3. **No history inspection.** Agents have no way to inspect a subagent's
   recent message history to decide whether to wait, dispatch a follow-up,
   or abandon a stalled task. The data exists in the SQLite
   `memory_session_messages` table but is unreachable from the CLI.

A fourth capability 鈥?cancelling a dispatched subagent task 鈥?was
considered but deferred (see "Future Work 鈥?Cancellation").

## Decision

Four decisions, grouped into three concerns plus one explicit deferral.

---

### D1 鈥?Disable rich rendering across all commands

The `modexctl` Typer application is constructed with:

```python
app = typer.Typer(
    name="modexctl",
    rich_markup_mode=None,            # no box-drawing in --help / errors
    pretty_exceptions_enable=False,   # plain-text exception traces
)
```

ANSI color highlights remain (Typer's default) 鈥?they are harmless to
agents reading stdout/stderr via `2>&1` capture and are useful for the
occasional human debugging the CLI directly. What is removed is the
**structural noise** (box characters `鈹€鈹傗攲鈹愨敂鈹榒, multi-line table layout,
markdown rendering in help text) that inflates token count and breaks
naive line-based parsing.

This applies uniformly to every command registered on the app, not just
`send`.

---

### D2 鈥?`send` gains `--invocation-id`, response surface, and quadrant-differentiated output

#### D2.1 鈥?`--invocation-id` parameter (subagent dispatch only)

`modexctl send` gains an optional `--invocation-id` parameter. Its
semantics mirror `send_to_agent` exactly:

| `--invocation-id` value | Behavior |
|---|---|
| omitted / `None` / empty string | mint a fresh 8-byte hex uuid; create a new subagent session |
| non-empty string `foo123` | attempt to resume subagent session `foo123.<agent_name>` |

**Session existence check** (subagent dispatch path only): before
dispatching, modexctl queries the workspace's `state.db`:

```sql
SELECT 1 FROM sessions WHERE session_id = ?
```

with `?` bound to `{invocation_id}.{agent_name}`. The check is
**best-effort and accepts a race**: agent may invoke `send` again before
the bot has finished registering the session from the previous send. The
documented contract is "only pass `--invocation-id` for sessions you have
already received a `<replied>` for" 鈥?i.e., once the bot has processed the
prior dispatch and registered the session.

**Not-found handling**: if `--invocation-id foo123` is passed but the
session does not exist in `sessions` (or its `parent_session_id` does not
match `MODEX_SESSION_ID` 鈥?these two cases are treated identically and not
distinguished), modexctl **mints a fresh uuid** (NOT `foo123`) and reports
this prominently in the output. `foo123` is never reused as a prefix for
a session it did not correspond to 鈥?that would silently mask an agent
mistake with a false "resumed" status.

The `parent_session_id` mismatch case is **not given special handling**:
in a correctly wired single-main-per-pool topology it cannot occur
legitimately, and treating it as "session not found" is the simplest
correct behavior. Deepening this into a dedicated error path is
out-of-scope.

This parameter is **only meaningful when `MODEX_COMM_KIND=subagent`**
(i.e., main 鈫?subagent dispatch within the same pool). For
`MODEX_COMM_KIND=normal` (cross-pool peer) and subagent 鈫?parent reply,
invocation_id is not a concept and the parameter is ignored.

#### D2.2 鈥?Quadrant-differentiated response output

The `send` command's stdout is differentiated by the combination of
`MODEX_COMM_KIND` and the target agent's kind (NATIVE vs. EXTERNAL).
The four quadrants:

**鈶?`normal` (cross-pool peer, target = NATIVE or EXTERNAL)**
```
Message delivered to '{to}'.
Peer will process asynchronously. No wait needed.
```
No `session_id`, no `invocation_id` 鈥?the peer relationship is fully
opaque to the sender. The target session is `{self_prefix}.{to}` per
ADR-0019 prefix-reuse, but this is routing-layer detail the agent does
not need.

**鈶?`subagent` dispatch, target = NATIVE**
```
Task dispatched to subagent '{to}'.
invocation_id: {invocation_id}
session_id: {session_id}
status: new_task | resumed | new_task (provided '{provided_id}' not found, created new)
output_path: {output_path}
trace_dir: {trace_dir}

Subagent will run asynchronously 鈥?wait for the <replied> block, do not poll.
```

**鈶?`subagent` dispatch, target = EXTERNAL**
```
Task dispatched to subagent '{to}'.
invocation_id: {invocation_id}
session_id: {session_id}
status: new_task | resumed | new_task (provided '{provided_id}' not found, created new)

Subagent will run asynchronously 鈥?wait for the <replied> block, do not poll.
```
Identical to 鈶?minus `output_path` and `trace_dir` 鈥?external coding
agents do not produce these framework-side artifacts (their transcript is
owned by the provider, not the framework; ADR-0022 D7).

**鈶?`subagent` 鈫?parent reply (subagent calling back to its parent)**
```
Reply delivered to parent (session: {parent_sid}).
Parent will continue its turn.
```
The parent's session_id is shown because the subagent is replying *into*
that session's inbox; it is the addressing target, not an opaque peer.

**Output contract**: no statement is made about *how the recipient will
reply* (no "the peer will use `modexctl send` to reply"). The agent
learns only that the message was delivered and what to do next (wait /
continue / no wait needed). The recipient's reply mechanism is the
recipient's concern 鈥?telling the sender about it leaks implementation
details across the agent boundary.

**Not-found status rendering** (applies to 鈶?and 鈶?when
`--invocation-id` was passed but the session was not found): the
`status:` line uses the form
`new_task (provided '{provided_id}' not found, created new)` and the
`invocation_id:` line shows the **newly minted** uuid (not `foo123`).
This makes the not-found case visually distinguishable from a normal
`new_task` and from `resumed`.

---

### D3 鈥?New `history` subcommand for inspecting subagent message history

#### D3.1 鈥?Surface

```
modexctl history --agent <name> --invocation-id <id> [--limit N]
```

Parameters:
- `--agent` (required): the subagent's name. Together with
  `--invocation-id` this forms the target `session_id =
  {invocation_id}.{agent}`.
- `--invocation-id` (required): the subagent invocation id. Required
  (not optional) because history is meaningless without identifying the
  exact subagent session.
- `--limit` (optional, default 3, max 10): number of messages to return.
  Values >10 are silently clamped to 10. Values 鈮? are rejected with a
  usage error.

The command is **only registered when `MODEX_COMM_KIND=subagent`** 鈥?history inspection is a subagent-governance operation, not a peer
operation. (Peer main agents' history is out-of-scope: it would require
cross-workspace reads and is not part of this ADR.)

#### D3.2 鈥?Data source and query

modexctl opens a short-lived `sqlite3` connection to
`<workspace>/.modex/state.db` (the same pattern as `modexctl send`'s
inbox delivery path) and queries `memory_session_messages`:

```sql
SELECT message_id, role, content, is_content_json, token_count,
       message_json, created_at, state
FROM memory_session_messages
WHERE scope_key = ?
ORDER BY created_at DESC
LIMIT ?;
```

- `scope_key` is computed as `RecordScope(session_id="{invocation_id}.{agent}").canonical()`.
  Because `SessionScope.extract()` only populates `session_id` and
  `model_dump(exclude_none=True)` drops the unset `pool` (the bot's
  `BotRecordScope.pool` field is `None` at the framework base layer per
  ADR-0028), the canonical form is exactly
  `{"session_id": "<invocation_id>.<agent>"}` 鈥?no MemorySystem
  assembly, no scope resolver, no pool knowledge required.
- `ORDER BY created_at DESC` (int ms, ADR-0029) is the stable ordering
  source. `seq` is NOT used: it is per-scope monotonic but its semantics
  differ between file and SQLite backends (`save_messages` re-sequences
  on file; `replace_active_messages` continues from `MAX(seq)+1` on
  SQLite). `created_at` is a real timestamp stable across backends.
- No `state` filter 鈥?**soft-deleted messages (`state='soft_deleted'`)
  are included**. The requirement is "true history", which includes
  messages that were compressed out by `prune_messages` /
  `retain_messages`. The `state` column is exposed via the
  `_deleted` marker on the assembled dict (mirroring
  `SqliteMessageStore.load_all_messages`), then stripped by the VO
  filter (D3.3).

**No FILE backend support.** modexctl is a bot-runtime tool; the bot's
default backend is SQLite (ADR-0023). If `state.db` does not exist or
the table is empty, the command prints `No history found for session
'{session_id}'.` and exits 0. There is no fallback to reading
`messages.jsonl` 鈥?that would require replicating
`DefaultScopedStorage`'s storage layout and would leak MemorySystem
internals into the CLI.

**Reuse of `_assemble_message`**: row-to-dict reassembly uses the
existing `modex_agent.persistence.adapters.message_store._assemble_message`
helper. This is a deliberate cross-module reuse of a private helper
rather than a reimplementation 鈥?the helper owns the
`ColumnProjection` round-trip logic (ADR-0030) and any divergence would
create silent data corruption. The function is imported as-is; if it is
later made public, the import path is the only change.

#### D3.3 鈥?VO (View Object) field filtering

The assembled `dict[str, Any]` is filtered through a whitelist before
serialization. This mirrors the Java VO pattern: only core fields are
exposed to the agent, internal markers (`_deleted`, `_pinned`,
`token_count`, `is_content_json`, `content_format`, `reasoning_content`)
are stripped.

The whitelist:

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

`content` is preserved verbatim 鈥?its type is `str | list[ContentPart] | None`
and modexctl does not coerce. The overwhelming majority of subagent
messages are text; the rare multimodal message round-trips as a list and
the agent is expected to handle both shapes.

`tool_calls` is a `list[ToolCall]`; each `ToolCall` carries its own
`id` / `type` / `function` (name + arguments) 鈥?these nested fields are
intrinsic to `ToolCall` and are NOT filtered by the outer whitelist.

#### D3.4 鈥?Output format: JSON Lines

Each message is serialized as one JSON object per line:

```
{"role":"user","content":"...","created_at":1721782800000,"message_id":"..."}
{"role":"assistant","content":"...","tool_calls":[...],"created_at":1721782801000,"message_id":"..."}
{"role":"tool","content":"...","tool_call_id":"...","tool_name":"...","created_at":1721782802000,"message_id":"..."}
```

`json.dumps(msg, ensure_ascii=False)` is used 鈥?it escapes embedded `\n`
to `\\n` so each message stays on one physical line. This is critical:
agent-side parsing splits on `\n`, and a `content` field containing a
literal newline would otherwise break the line-based protocol.

The output ordering is **newest first** (matches the SQL `ORDER BY
created_at DESC`); the agent receives messages in reverse chronological
order. This is consistent with "show me the latest N" semantics 鈥?the
first line is the most recent message.

**No header, no footer, no separator lines** 鈥?the entire stdout is
strictly N lines of JSON (or zero, when no history exists), terminated by
the standard "No history found" message on stderr. This makes the output
directly pipeable to `jq`, `python -c "for line in sys.stdin: ..."`, or
any line-oriented parser.

---

### D4 鈥?Cancellation: deferred (Future Work)

A `cancel` subcommand was considered and is **not implemented** in this
ADR. The requirement and background are recorded here so a future design
can pick it up without re-deriving the context.

#### Requirement (original)

> Add a `cancel` operation for subagent tasks. Reuse the existing
> cancellation mechanism. CLI takes parameters similar to `history`
> (`--agent`, `--invocation-id`). Because subagents have a dedicated
> hook that notifies the parent of task status, the CLI must inform the
> caller that a cancellation notification will arrive subsequently, so
> the agent does not mistake it for an unsolicited message.

#### Background (why it is hard)

The cancellation mechanism is `ControlCommand(type=CANCEL_TURN,
scope=ControlScope(session_id, agent_id, turn_id))` delivered via
`ControlChannel`. The only production implementation is
`InMemoryControlChannel` 鈥?a process-internal `dict[session_id 鈫?dict[type 鈫?deque]]` guarded by an `asyncio.Lock`.

**There is no cross-process delivery path.** All current cancel sources
(WebUI pause button, IM `/stop`, internal scheduler timeouts) inject
commands from inside the bot process. `ControlChannel` has no
`deliver()`-style synchronous method analogous to `InboxMQ.deliver()`
which ADR-0022 added precisely to enable cross-process inbox writes.

To support `modexctl cancel`, three pieces of infrastructure would be
needed:

1. **A cross-process write surface** for control commands. Two options:
   - **a) Mirror ADR-0022's inbox pattern**: add `deliver_sync()` to
     the `ControlChannel` ABC, implement it in a new
     `SqliteControlChannel` (or as a side table on the existing
     `SqliteInboxMQ`), add a poller on the bot side that drains the
     table and re-injects into `InMemoryControlChannel`.
   - **b) Reuse the inbox path**: send a special `<cancel_request>`
     envelope through the existing `InboxMQ.deliver()`, add a subagent
     hook that recognizes it and calls `pool.shutdown_agent`.
2. **Effective cancellation semantics at the ReAct loop level**. A
   `CANCEL_TURN` command arriving mid-LLM-call or mid-tool-execution is
   only honored at the next drain checkpoint. Long-running tool calls
   (terminal, MCP) may not observe the cancel for an unbounded time.
   This is an existing framework behavior that the CLI would expose
   directly 鈥?not a new problem, but one that must be documented in the
   CLI's response.
3. **`SubagentAutoSendHook` integration**. The hook already fires on
   subagent turn end (including cancelled) and writes a `<replied>`
   envelope to the parent's inbox. The CLI's response must explicitly
   tell the caller "a cancellation notification will arrive via the
   normal `<replied>` channel 鈥?do not treat it as unsolicited" to
   avoid the agent misinterpreting the eventual acknowledgement.

#### Why deferred

- Each piece above is non-trivial; (1a) alone is an ABC change plus a
  new persistence adapter plus a new poller 鈥?comparable in scope to
  ADR-0022's inbox deliver path.
- The ROI is low: an agent that observes a stalled subagent (via
  `modexctl history`) has alternatives (send a follow-up message that
  causes a natural timeout, wait for the subagent's
  `max_iterations` cap, or simply abandon the session).
- The original requirement explicitly marked this as a "challenge item
  鈥?skip if hard." It is hard.

A future ADR should pick this up by selecting between options (1a) and
(1b) above, defining the poller's drain cadence (if 1a), and specifying
the CLI's response template (which must include the
"cancellation notification will arrive" warning the original requirement
calls out).

---

## Consequences

### Positive

- **Agent ergonomics.** Output is line-stable, JSON-parseable where
  structured, and free of box-drawing noise. Agents can reliably
  `json.loads` each `history` line and pattern-match the four `send`
  output quadrants.
- **Symmetry with `send_to_agent`.** The `--invocation-id` parameter and
  the `invocation_id` / `session_id` / `status` response surface bring
  the CLI to parity with the framework tool. Agents learning either
  surface transfer to the other for free.
- **Visibility without coupling.** `history` lets agents inspect
  subagent progress without depending on WebUI components, without
  assembling a `MemorySystem`, and without reaching into
  `MessageStore` ABCs. The data path is a single SQLite `SELECT` plus
  the existing `_assemble_message` helper.
- **True history semantics.** Including `state='soft_deleted'` rows
  means agents see the full record of what happened, not just the
  post-compaction view. This matches the "true history" requirement.
- **Extensible VO.** The whitelist is a single frozenset; adding or
  removing a field is a one-line change with no model surgery.

### Negative

- **SQLite-only.** Workspaces on the FILE backend (the framework
  default) get `No history found` even if `messages.jsonl` exists. This
  is a deliberate trade for keeping modexctl free of MemorySystem
  internals; affected users can switch backends (ADR-0023) or use the
  WebUI transcript view.
- **Not-found race.** The session-existence check in D2.1 has a
  TOCTOU window: a second `send --invocation-id X` invoked before the
  bot has registered session X from the first send will mint a new
  uuid. The contract puts the responsibility on the agent ("only pass
  `--invocation-id` for sessions you have received a `<replied>`
  for"). This is documented but not enforced.
- **No cancellation.** Agents cannot proactively cancel a stalled
  subagent via the CLI; they must wait for natural termination or use
  in-band alternatives. See D4.
- **Quadrant output is parseable but not machine-typed.** The four
  `send` output templates are plain text, not JSON. An agent that
  wants structured data must pattern-match. (Making `send` output JSON
  was considered and rejected: the four quadrants are few, stable, and
  the readability benefit for human debugging outweighs the marginal
  parsing cost. `history` 鈥?the high-volume command 鈥?is already
  JSON Lines.)

### Neutral

- **Reuse of `_assemble_message` across module boundaries.** The helper
  is module-private (`_` prefix) but stable. If it later moves or
  becomes public, the import path changes; the call shape does not.

## Alternatives considered

- **Keep rich rendering, just for help.** Rejected: agents invoking
  `--help` (e.g., to discover available subcommands when
  `MODEX_COMM_KIND` env-gates the registration) would still receive
  box-drawing noise.
- **Make `send` output JSON.** Rejected for the four-quadrant templates
  (few, stable, human-readable); accepted for `history` (high-volume,
  structured).
- **Support FILE backend in `history`.** Rejected: replicating
  `DefaultScopedStorage`'s layout in the CLI leaks MemorySystem
  internals. The bot's default is SQLite; FILE users have the WebUI
  transcript view.
- **Use `seq` instead of `created_at` for ordering.** Rejected: `seq`
  semantics differ between backends; `created_at` is a real timestamp
  stable across both.
- **Filter soft-deleted messages out of `history`.** Rejected: the
  requirement explicitly called for "true history" including soft-deleted
  entries.
- **Validate `parent_session_id` mismatch as a distinct error.**
  Rejected: in a correctly wired single-main-per-pool topology it
  cannot occur; treating it as "not found" is the simplest correct
  behavior. Deepening this into a dedicated error path is out-of-scope.
- **Implement cancellation now.** Rejected: see D4. Each piece (ABC
  change, poller, ReAct checkpoint semantics, hook integration) is
  non-trivial and the original requirement explicitly allowed
  deferring. Recorded for future design.

## Relationships

- **Builds on**: ADR-0022 (modexctl + external coding agent integration),
  ADR-0019 (peer prefix-reuse), ADR-0023 (SQLite persistence),
  ADR-0028 (RecordScope canonical), ADR-0029 (epoch-ms timestamps),
  ADR-0030 (ColumnProjection / `_assemble_message`), ADR-0015 (inbox).
- **Does not modify**: ADR-0022's topology (external coding agents remain
  NORMAL main agents of their own pools), ADR-0019's prefix-reuse rule,
  the `ControlChannel` ABC (D4 is deferred).
- **Future**: a follow-up ADR for cancellation (D4) will need to extend
  `ControlChannel` (option 1a) or add an inbox-routed cancel envelope
  (option 1b).
