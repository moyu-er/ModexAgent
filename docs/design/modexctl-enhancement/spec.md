Status: ready-for-agent

# modexctl Agent Self-Governance Enhancement

Related ADR: [ADR-0035](../../adr/0035-modexctl-agent-self-governance-enhancement.md)
Related PRD: [PRD.md](PRD.md)
Related glossary: [glossary.md](glossary.md)

## Problem Statement

I am an external coding agent (Pi, OpenCode, …) running as a NORMAL main
agent of my own pool, integrated into the ModexAgent multi-agent topology
via ADR-0022. The framework gives me `modexctl` as my only interface to
the rest of the topology — it's a stateless, env-gated CLI I invoke from
bash to write into other pools' inboxes.

Three things make `modexctl` hard to use from bash today:

1. **Help and error output is noisy.** Typer's default rich rendering
   paints box-drawing borders (`─│┌┐└┘├┤┬┴┼`) and multi-line tables
   around `--help` and exception traces. I read `2>&1` output into my
   context window, and every box character is token noise that also
   breaks naive line-based parsing.
2. **Subagent dispatch is asymmetric with the framework tool.** The
   in-process `send_to_agent` tool that native ReAct agents use accepts
   an `invocation_id` parameter (new task vs. resume) and returns a
   structured `AgentSendResult` with `invocation_id`, `session_id`,
   `created_new_task`, `output_path`, and `trace_dir`. `modexctl send`
   accepts none of these: it always mints a fresh invocation_id, has no
   way to resume a subagent session, and emits a single fixed string
   that tells me nothing about what was created. I cannot reliably tell
   whether I just dispatched a new task or resumed an existing one.
3. **I cannot inspect subagent progress.** Once I dispatch a task to a
   subagent, I receive a `<replied>` block when it finishes — but until
   then I have zero visibility. If a subagent stalls (e.g. stuck in a
   tool call, hit `max_iterations`, or just slow), I cannot tell whether
   to wait, follow up, or abandon. The message history exists in my
   workspace's SQLite `state.db`, but `modexctl` exposes no way to read
   it.

A fourth capability — cancelling a stalled subagent — was considered but
is genuinely hard (the cancellation mechanism is process-internal with
no cross-process surface, unlike the inbox's `deliver()`). It is
deferred to a future ADR but recorded here so the context is not lost.

## Solution

From my perspective as an external coding agent invoking `modexctl`
from bash:

1. **All `modexctl` output is plain text.** `--help` and exception
   traces use plain text — no box-drawing, no multi-line tables. ANSI
   color codes are fine (harmless when I capture `2>&1`).
2. **`modexctl send` accepts `--invocation-id` and reports what
   happened.** When I dispatch to a subagent, the CLI tells me the
   invocation_id, session_id, whether it was new or resumed, and (for
   native subagents) where the output and trace files live. The output
   format is differentiated across four quadrants (cross-pool peer,
   subagent-native, subagent-external, subagent→parent reply) so I
   only see fields that are meaningful for my situation. The CLI does
   not lecture me about how the recipient will reply — that's the
   recipient's concern.
3. **`modexctl history` lets me inspect a subagent's recent messages.**
   I pass `--agent <name> --invocation-id <id>` and get back the last
   N messages (default 3, max 10) as one JSON object per line. The
   output is strictly JSON Lines — no headers, no footers, no
   separators — so I can pipe it to `jq` or parse line-by-line
   directly. Soft-deleted messages are included (true history), but
   internal markers (`_deleted`, `_pinned`, `token_count`, etc.) are
   stripped by a VO whitelist so I see only the core fields.

## User Stories

1. As an external coding agent, I want `modexctl --help` to print plain
   text without box-drawing characters, so that I can read the help
   output into my context window without token noise.
2. As an external coding agent, I want `modexctl <unknown-command>`
   exception traces to be plain text without rich panels, so that I
   can parse the error message from `2>&1` without filtering ANSI
   structural characters.
3. As an external coding agent, I want every `modexctl` subcommand's
   `--help` output to be plain text, so that I can discover available
   commands uniformly without per-command noise.
4. As an external coding agent dispatching a new task to a subagent, I
   want to omit `--invocation-id` and have `modexctl send` mint a fresh
   uuid, so that I get a brand new subagent session without having to
   generate ids myself.
5. As an external coding agent dispatching a new task to a subagent, I
   want the CLI response to include the minted `invocation_id`, the
   `session_id`, a `status: new_task` line, and (for native
   subagents) the `output_path` and `trace_dir`, so that I can record
   the task id and inspect progress later.
6. As an external coding agent resuming a previously-dispatched
   subagent task, I want to pass `--invocation-id <id>` and have
   `modexctl send` resume the existing `{id}.{agent_name}` session,
   so that the subagent picks up where it left off with full context.
7. As an external coding agent resuming a subagent task, I want the
   CLI response to show `status: resumed` with the invocation_id and
   session_id, so that I can confirm the resume succeeded.
8. As an external coding agent that passed a stale `--invocation-id`
   (the session was cleaned up, or I misremembered the id), I want
   `modexctl send` to mint a fresh uuid and prominently tell me
   `status: new_task (provided '<id>' not found, created new)`, so
   that I do not silently mistake a fresh session for a resumed one.
9. As an external coding agent that passed a stale
   `--invocation-id`, I want the `invocation_id:` line in the
   response to show the newly minted uuid (not the stale id I
   passed), so that I update my records with the id that was
   actually used.
10. As an external coding agent, I want passing `--invocation-id ""`
    (empty string) to behave identically to omitting the flag, so
    that I do not have to special-case empty values in my bash
    scripting.
11. As an external coding agent sending a cross-pool peer message
    (`MODEX_COMM_KIND=normal`), I want the response to contain only
    `Message delivered to '{to}'.` and an async-wait hint, so that I
    am not confused by subagent-specific fields that do not apply to
    peer communication.
12. As an external coding agent sending a cross-pool peer message, I
    want the response to NOT include the target `session_id`, so that
    the peer relationship stays opaque to me (the target session is
    the routing layer's concern, not mine).
13. As an external coding agent sending a cross-pool peer message, I
    want the response to NOT include `invocation_id`, so that I do
    not mistakenly believe peer communication has subagent semantics.
14. As an external coding agent sending a subagent→parent reply
    (`MODEX_COMM_KIND=subagent` targeting my parent), I want the
    response to show the parent's `session_id` (because that is the
    inbox I am writing into) and a "parent will continue" hint, so
    that I understand my reply landed in the right place.
15. As an external coding agent, I want no `modexctl send` output
    template to mention how the recipient will reply (no "the peer
    will use `modexctl send` to reply"), so that I am not misled
    about the recipient's mechanism — that is the recipient's
    concern.
16. As an external coding agent dispatching to an external-coding
    subagent (target kind = EXTERNAL_CODING), I want the response to
    omit `output_path` and `trace_dir` (because external coding
    agents do not produce framework-side artifacts), so that I am
    not confused by paths that do not exist.
17. As an external coding agent, I want `--invocation-id` to be
    silently ignored (not error) when `MODEX_COMM_KIND=normal`, so
    that I can use the same bash invocation shape across quadrants
    without conditional logic.
18. As an external coding agent, I want `modexctl history --agent
    <name> --invocation-id <id>` to print the last 3 messages from
    that subagent session as JSON Lines (one message per line, newest
    first), so that I can inspect recent progress without leaving
    bash.
19. As an external coding agent, I want to pass `--limit N` to
    `modexctl history` to control how many messages are returned
    (default 3, max 10), so that I can widen or narrow my view as
    needed.
20. As an external coding agent, I want `--limit 100` to be silently
    clamped to 10, so that an over-large request does not error but
    returns a bounded amount of data.
21. As an external coding agent, I want `--limit 0` or `--limit -1`
    to be a usage error, so that I notice when I pass a nonsensical
    limit.
22. As an external coding agent, I want each `modexctl history`
    output line to be valid JSON parseable by `json.loads`, so that
    I can use standard JSON tooling.
23. As an external coding agent, I want each `modexctl history`
    output line to contain only the core fields (`role`, `content`,
    `tool_calls`, `tool_call_id`, `tool_name`, `name`, `created_at`,
    `message_id`) and NOT internal markers (`_deleted`, `_pinned`,
    `token_count`, `is_content_json`, `content_format`,
    `reasoning_content`), so that I am not exposed to framework
    internals.
24. As an external coding agent, I want `modexctl history` to
    include soft-deleted messages (messages that were compressed out
    by `prune_messages` / `retain_messages`), so that I see the true
    history of what happened in the subagent session, not just the
    post-compaction view.
25. As an external coding agent, I want `modexctl history` to order
    results by `created_at DESC` (newest first), so that the first
    line I read is the most recent message.
26. As an external coding agent, I want `modexctl history` output to
    have no headers, no footers, and no separator lines — strictly N
    lines of JSON — so that I can pipe the output directly to `jq`
    or line-by-line parsing.
27. As an external coding agent, I want `content` fields containing
    literal newlines to be JSON-escaped (`\n` → `\\n`), so that
    each message stays on one physical line and my line-based
    parsing does not break.
28. As an external coding agent, I want `content` to be preserved
    verbatim whether it is a string or a list (multimodal), so that
    I do not lose structure on the rare multimodal message.
29. As an external coding agent whose workspace has no `state.db`
    yet (no history exists for the session), I want `modexctl
    history` to print `No history found for session '{session_id}'.`
    to stderr and exit 0, so that I can distinguish "no data" from
    "command failed".
30. As an external coding agent, I want `modexctl history` to only
    be registered as a subcommand when `MODEX_COMM_KIND=subagent`,
    so that peer main agents do not see a command that has no
    meaningful semantics for them.
31. As an external coding agent on a FILE-backend workspace (not
    SQLite), I want `modexctl history` to print "No history found"
    rather than read `messages.jsonl`, so that the CLI does not
    silently break — I understand the bot's default is SQLite and I
    can switch backends if I need history.
32. As an external coding agent, I want `modexctl history`'s
    `--agent` and `--invocation-id` parameters to both be required,
    so that I cannot accidentally invoke history without identifying
    the exact subagent session.
33. As an external coding agent, I want to be able to discover the
    `history` subcommand via `modexctl --help` (when
    `MODEX_COMM_KIND=subagent`), so that I do not need out-of-band
    documentation to find it.
34. As an external coding agent, I want the `send` and `history`
    subcommands' env-gating to use the same `MODEX_COMM_KIND=subagent`
    predicate, so that the available command surface is consistent
    across subagent and peer contexts.
35. As an external coding agent dispatching to a subagent, I want
    the CLI to validate the target session's existence by querying
    the `sessions` table (`SELECT 1 FROM sessions WHERE session_id =
    ?`), so that I get accurate `new_task` vs. `resumed` status.
36. As an external coding agent, I accept that the session-existence
    check has a TOCTOU race (a second `send --invocation-id X` before
    the bot registers session X will mint a new uuid), and I will
    only pass `--invocation-id` for sessions I have already received
    a `<replied>` for.
37. As an external coding agent, I want the
    `parent_session_id`-mismatch case (target session exists but
    belongs to a different parent) to be treated identically to
    "session not found" (mint fresh uuid, report "not found, created
    new"), so that I do not receive a confusing dedicated error for
    a situation that cannot legitimately occur in a
    single-main-per-pool topology.
38. As a future designer of the cancellation feature, I want the
    full requirement and background for `modexctl cancel` recorded
    in ADR-0035 D4, so that I can pick up the design without
    re-deriving the context.

## Implementation Decisions

### Modules to be built/modified

- **`modexctl/main.py`** — Typer app construction (D1: disable rich
  rendering); `send` command (D2: add `--invocation-id`, quadrant
  output); new `history` command (D3).
- **New module(s) under `modexctl/`** — history query + VO filter +
  quadrant output formatting. Keep `main.py` focused on CLI plumbing;
  extract pure functions into a sibling module so they can be unit
  tested without spinning up the Typer app.

### D1 — Disable rich rendering

The `modexctl` Typer application is constructed with
`rich_markup_mode=None` (no box-drawing in `--help` / error rendering)
and `pretty_exceptions_enable=False` (plain-text exception traces).
ANSI color codes remain (Typer default) — harmless to agents reading
`2>&1`. This applies uniformly to every command registered on the app.

### D2 — `send` enhancement

#### `--invocation-id` parameter (subagent dispatch only)

A new optional `--invocation-id` parameter on `modexctl send`. Its
semantics mirror `send_to_agent` exactly:

- omitted / `None` / empty string → mint a fresh 8-byte hex uuid;
  create a new subagent session
- non-empty string `foo123` → attempt to resume subagent session
  `foo123.<agent_name>`

**Session existence check** (subagent path only): before dispatching,
modexctl queries the workspace's `state.db` with a short-lived stdlib
`sqlite3` connection (the same pattern as the existing inbox delivery
path):

```sql
SELECT 1 FROM sessions WHERE session_id = ?
```

with `?` bound to `{invocation_id}.{agent_name}`.

- Exists → `status: resumed`
- Not exists → mint a **fresh** uuid (NOT the provided one) →
  `status: new_task (provided '{provided_id}' not found, created new)`
- `parent_session_id` mismatch → treated identically to "not exists"
  (not deepened; cannot legitimately occur in single-main-per-pool
  topology)

**Race accepted**: a TOCTOU window exists between the existence check
and the bot's session registration. The contract places responsibility
on the caller: "only pass `--invocation-id` for sessions you have
received a `<replied>` for."

Only meaningful when `MODEX_COMM_KIND=subagent`. Silently ignored
(not an error) when `MODEX_COMM_KIND=normal`.

#### Quadrant-differentiated output

The `send` command's stdout is differentiated by the combination of
`MODEX_COMM_KIND` and the target agent's kind (NATIVE vs.
EXTERNAL_CODING). Four quadrants:

**① `normal` (cross-pool peer, target = NATIVE or EXTERNAL)**
```
Message delivered to '{to}'.
Peer will process asynchronously. No wait needed.
```
No `session_id`, no `invocation_id` — the peer relationship is fully
opaque to the sender.

**② `subagent` dispatch, target = NATIVE**
```
Task dispatched to subagent '{to}'.
invocation_id: {invocation_id}
session_id: {session_id}
status: new_task | resumed | new_task (provided '{provided_id}' not found, created new)
output_path: {output_path}
trace_dir: {trace_dir}

Subagent will run asynchronously — wait for the <replied> block, do not poll.
```

**③ `subagent` dispatch, target = EXTERNAL_CODING**
```
Task dispatched to subagent '{to}'.
invocation_id: {invocation_id}
session_id: {session_id}
status: new_task | resumed | new_task (provided '{provided_id}' not found, created new)

Subagent will run asynchronously — wait for the <replied> block, do not poll.
```
Identical to ② minus `output_path` and `trace_dir`.

**④ subagent → parent reply**
```
Reply delivered to parent (session: {parent_sid}).
Parent will continue its turn.
```

**Output contract**: no statement about *how the recipient will reply*.
The agent learns only that the message was delivered and what to do
next (wait / continue / no wait needed).

**Not-found status rendering** (② and ③, when `--invocation-id` was
passed but the session was not found): the `status:` line uses the form
`new_task (provided '{provided_id}' not found, created new)` and the
`invocation_id:` line shows the **newly minted** uuid (not the provided
id). This makes the not-found case visually distinguishable from a
normal `new_task` and from `resumed`.

### D3 — `history` subcommand

#### Surface

```
modexctl history --agent <name> --invocation-id <id> [--limit N]
```

- `--agent` (required)
- `--invocation-id` (required)
- `--limit` (optional, default 3, max 10; >10 clamped, ≤0 rejected as
  usage error)

**Only registered when `MODEX_COMM_KIND=subagent`.** Peer main agents'
history is out-of-scope.

#### Data source and query

Short-lived stdlib `sqlite3` connection to
`<workspace>/.modex/state.db`:

```sql
SELECT message_id, role, content, is_content_json, token_count,
       message_json, created_at, state
FROM memory_session_messages
WHERE scope_key = ?
ORDER BY created_at DESC
LIMIT ?;
```

- `scope_key` is computed as
  `RecordScope(session_id="{invocation_id}.{agent}").canonical()`. Because
  `SessionScope.extract()` only populates `session_id` and
  `model_dump(exclude_none=True)` drops the unset `pool` (the bot's
  `BotRecordScope.pool` is `None` at the framework base layer per
  ADR-0028), the canonical form is exactly
  `{"session_id": "<invocation_id>.<agent>"}`. No MemorySystem
  assembly, no scope resolver, no pool knowledge required.
- `ORDER BY created_at DESC` (int ms, ADR-0029). `seq` is NOT used —
  its semantics differ between backends.
- **No `state` filter** — soft-deleted messages included ("true
  history").
- **No FILE backend support.** If `state.db` is missing or the table is
  empty → print `No history found for session '{session_id}'.` to
  stderr and exit 0.
- **Reuse `_assemble_message`** from
  `modex_agent.persistence.adapters.message_store` — owns the
  ColumnProjection (ADR-0030) round-trip; reimplementation would risk
  silent data corruption.

#### VO (View Object) field whitelist

The assembled `dict[str, Any]` is filtered through a whitelist before
serialization. Stripped: `_deleted`, `_pinned`, `token_count`,
`is_content_json`, `content_format`, `reasoning_content`. `content` is
preserved verbatim (`str | list[ContentPart] | None`).

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

This whitelist is a single frozenset — adding or removing a field is a
one-line change with no model surgery. Mirrors the Java VO pattern:
externalize a stable, minimal projection of an internal rich object.
`tool_calls` is a `list[ToolCall]`; each `ToolCall` carries its own
`id` / `type` / `function` (name + arguments) — these nested fields are
intrinsic to `ToolCall` and are NOT filtered by the outer whitelist.

#### Output format: JSON Lines

Each message is serialized as one JSON object per line:
`json.dumps(msg, ensure_ascii=False)` — escapes embedded `\n` → `\\n`
so each message stays on one physical line. Ordering: **newest first**
(matches SQL `ORDER BY created_at DESC`). No header, no footer, no
separator lines — strictly N lines of JSON (or zero + stderr "No
history found"). Directly pipeable to `jq` or any line-oriented parser.

### D4 — Cancellation: deferred (Future Work)

A `cancel` subcommand is **not implemented** in this spec. The
requirement and background are recorded in ADR-0035 D4 so a future
design can pick it up without re-deriving the context.

**Requirement (original)**: Add a `cancel` operation for subagent
tasks. Reuse the existing `CANCEL_TURN` control channel mechanism. CLI
takes parameters similar to `history` (`--agent`, `--invocation-id`).
Because subagents have a dedicated hook (`SubagentAutoSendHook`) that
notifies the parent of task status, the CLI must inform the caller that
a cancellation notification will arrive subsequently (via the normal
`<replied>` channel), so the agent does not mistake it for an
unsolicited message.

**Why it is hard**: The cancellation mechanism is
`ControlCommand(type=CANCEL_TURN, scope=ControlScope(session_id,
agent_id, turn_id))` delivered via `ControlChannel`. The only production
implementation is `InMemoryControlChannel` — a process-internal
`dict[session_id → dict[type → deque]]` guarded by an `asyncio.Lock`.
**There is no cross-process delivery path.** All current cancel sources
(WebUI pause button, IM `/stop`, internal scheduler timeouts) inject
commands from inside the bot process. `ControlChannel` has no
`deliver()`-style synchronous method analogous to `InboxMQ.deliver()`
which ADR-0022 added precisely to enable cross-process inbox writes.

Three pieces of infrastructure would be needed:

1. A cross-process write surface for control commands. Two options:
   - Mirror ADR-0022's inbox pattern: add `deliver_sync()` to the
     `ControlChannel` ABC, implement it in a new
     `SqliteControlChannel`, add a bot-side poller that drains the
     table and re-injects into `InMemoryControlChannel`.
   - Reuse the inbox path: send a special `<cancel_request>` envelope
     through the existing `InboxMQ.deliver()`, add a subagent hook
     that recognizes it and calls `pool.shutdown_agent`.
2. Effective cancellation semantics at the ReAct loop level. A
   `CANCEL_TURN` command arriving mid-LLM-call or mid-tool-execution
   is only honored at the next drain checkpoint. Long-running tool
   calls may not observe the cancel for an unbounded time. This is an
   existing framework behavior the CLI would expose directly — not a
   new problem, but one that must be documented.
3. `SubagentAutoSendHook` integration. The hook already fires on
   subagent turn end (including cancelled) and writes a `<replied>`
   envelope to the parent's inbox. The CLI's response must explicitly
   tell the caller "a cancellation notification will arrive via the
   normal `<replied>` channel — do not treat it as unsolicited."

**Why deferred**: Each piece above is non-trivial; (1a) alone is an ABC
change plus a new persistence adapter plus a new poller — comparable in
scope to ADR-0022's inbox deliver path. The ROI is low: an agent that
observes a stalled subagent (via `modexctl history`) has alternatives
(send a follow-up message causing natural timeout, wait for
`max_iterations`, abandon the session). The original requirement
explicitly marked this as a "challenge item — skip if hard."

### Architectural decisions

- **Strictly additive.** No existing `modexctl send` behavior changes
  except the output format (which becomes quadrant-differentiated
  rather than a fixed string). The `agents` command is untouched.
- **Reuse, don't reimplement.** `_assemble_message` is reused across
  module boundaries (it is module-private but stable). If it later
  moves or becomes public, the import path changes; the call shape
  does not.
- **No new ABCs.** D3 is a CLI-only feature; it does not extend
  `MessageStore` or any other framework ABC. D4 would have, but is
  deferred.
- **No new persistence tables.** D3 reads existing
  `memory_session_messages` and `sessions` tables; D2 reads
  `sessions`. No schema changes.
- **Cross-process pattern reuse.** D2's session-existence check and
  D3's history query both use short-lived stdlib `sqlite3` connections
  to `state.db` — the same pattern ADR-0022 established for
  `SqliteInboxMQ.deliver()`.

### API contracts

- `modexctl send --to <name> [--content <text> | --content-file <path> |
  --stdin] [--invocation-id <id>]` — stdout is one of the four
  quadrant templates.
- `modexctl history --agent <name> --invocation-id <id> [--limit N]`
  — stdout is N lines of JSON (or zero lines + stderr "No history
  found").
- `modexctl agents` — unchanged.
- `modexctl --help` / `modexctl <cmd> --help` — plain text, no
  box-drawing.
- Env gating: `send` and `agents` require the five `MODEX_COMM_*` env
  vars (unchanged); `history` additionally requires
  `MODEX_COMM_KIND=subagent` (new gating, independent of the workflow
  env gating).

## Testing Decisions

### What makes a good test

Only test external behavior, not implementation details. For a CLI,
"external behavior" means:

- The stdout / stderr / exit code observed by a bash caller.
- The side effects on the filesystem / SQLite database that the bot
  process would observe.

Internal refactors (extracting a helper function, renaming a private
variable, switching from one JSON serialization library to another)
must not require test changes.

### Test seam

**Single high seam**: `typer.testing.CliRunner.invoke(build_app(),
[...])` against a `tmp_path` workspace with a real `state.db`. This
is the existing pattern in `tests/unit/cli/modexctl/test_main.py`
(e.g., `test_valid_runtime_writes_target_inbox`,
`test_send_subagent_kind_routes_to_parent_session_id_directly`).

Assertions are of two kinds:
- **Stdout/stderr/exit_code** — the CLI's external contract.
- **`sqlite3.connect(state.db).execute(...)`** — direct verification
  of the database side effect (message landed in inbox, session row
  exists, etc.). This is the same pattern as the existing
  `test_valid_runtime_writes_target_inbox`.

**Auxiliary low seam**: direct calls to module-level pure functions
for edge-case coverage that would be awkward to drive through the
CLI. This is the existing pattern of `TestParsePoolMap`,
`TestBuildInboxLine`, `TestComputeTargetSessionId`,
`TestResolveTargetPool`, `TestParseTargets` in `test_main.py`.
Candidates for this spec:
- VO whitelist filter function (input dict with all fields → output
  dict with only the 8 whitelisted fields).
- Scope-key construction (input `invocation_id` + `agent_name` →
  expected canonical JSON).
- Quadrant output formatter (input dict → expected template string).

**No new seams introduced.** Both seams above already exist in the
codebase.

### Modules to be tested

- `modexctl/main.py` (Typer app, `send`, `agents`).
- New `modexctl/` module(s) for `history` query, VO filter, quadrant
  formatter (pure functions, unit-tested directly).

### Prior art

- `tests/unit/cli/modexctl/test_main.py` — the canonical prior art.
  Every test class there is a model for one aspect of this spec:
  - `TestUnifiedCommGate` — env-gating (model for `history`
    `MODEX_COMM_KIND=subagent` gating).
  - `TestSendCommand` — full CLI invocation + `state.db` side-effect
    verification (model for D2 tests).
  - `TestParsePoolMap` / `TestBuildInboxLine` / `TestComputeTargetSessionId`
    — pure-function unit tests (model for VO filter / scope-key /
    quadrant formatter tests).
- `tests/unit/cli/modexctl/test_sqlite_persistence_unification.py` —
  cross-process scope_key matching (relevant to D3's scope_key
  construction; ensures modexctl's `RecordScope(session_id=...).canonical()`
  matches what the bot wrote).
- `tests/unit/cli/modexctl/test_external_coding_communication.py`,
  `test_cross_pool_peer_messaging.py`,
  `test_parent_session_id_propagation.py` — quadrant-specific routing
  prior art (model for the four-quadrant output differentiation).

### Specific test scenarios

**D1 (disable rich rendering)**:
- `runner.invoke(app, ["--help"]).output` contains no box-drawing
  characters (`─│┌┐└┘├┤┬┴┼`).
- `runner.invoke(app, ["send", "--invalid-flag"]).output` exception
  trace is plain text (no rich panels).
- Existing `send` / `agents` commands still function with identical
  behavior (modulo the new D2.2 output for `send`).

**D2 (send enhancement)**:
- No `--invocation-id`, `MODEX_COMM_KIND=subagent` → mints new uuid,
  dispatches, prints quadrant ② or ③ output with `status: new_task`.
- `--invocation-id foo123`, session `foo123.<agent>` exists in
  `sessions` table → prints `status: resumed`, `invocation_id: foo123`.
- `--invocation-id foo123`, session does NOT exist → mints new uuid,
  prints `status: new_task (provided 'foo123' not found, created
  new)`, `invocation_id:` shows the new uuid (not `foo123`).
- `MODEX_COMM_KIND=normal` → prints quadrant ① (no `session_id`, no
  `invocation_id`).
- subagent → parent reply path → prints quadrant ④.
- No output template mentions "the peer will use `modexctl send`" or
  any recipient-reply-mechanism statement.
- `--invocation-id ""` behaves identically to omitting the flag.
- `--invocation-id` silently ignored (not error) when
  `MODEX_COMM_KIND=normal`.

**D3 (history)**:
- `--agent researcher --invocation-id abc12345` → up to 3 JSON Lines,
  newest first.
- Each line `json.loads`-parseable.
- Each line contains only fields in `_HISTORY_VO_FIELDS` — no
  `_deleted`, `_pinned`, `token_count`, `is_content_json`,
  `content_format`, `reasoning_content`.
- `--limit 5` returns up to 5. `--limit 100` clamped to 10. `--limit
  0` / `--limit -1` → usage error.
- Soft-deleted messages (`state='soft_deleted'`) included.
- Missing `state.db` / empty table → `No history found for session
  '{session_id}'.` to stderr, exit 0.
- `MODEX_COMM_KIND != subagent` → `history` not registered (unknown
  command).
- `content` with literal `\n` → serialized as `\\n` (line stays one
  physical line).

**D4 (cancellation)**:
- ADR-0035 D4 contains the full requirement and background.
- No `cancel` subcommand is registered.

## Out of Scope

- **Cancellation implementation (D4).** The requirement and background
  are recorded in ADR-0035 D4 for a future ADR to pick up. The three
  pieces of infrastructure needed (cross-process write surface, ReAct
  checkpoint semantics, `SubagentAutoSendHook` integration) are each
  non-trivial and comparable in scope to ADR-0022's inbox deliver path.
- **FILE backend support for `history`.** modexctl is a bot-runtime
  tool; the bot's default backend is SQLite (ADR-0023). FILE-backend
  workspaces get "No history found" even if `messages.jsonl` exists.
  Replicating `DefaultScopedStorage`'s layout in the CLI would leak
  MemorySystem internals.
- **Cross-workspace history reads.** `history` is scoped to the current
  workspace's `state.db`. Reading another workspace's history is
  out-of-scope.
- **Peer main agent history.** `history` is subagent-only
  (`MODEX_COMM_KIND=subagent` required). Peer main agents' history is
  out-of-scope.
- **Modifying the `send_to_agent` framework tool.** The framework
  tool's `SubagentDispatchStrategy` does not validate
  `parent_session_id` against the caller — this is a known framework
  gap, not in scope for this spec. The CLI's behavior is independent.
- **Modifying the `ControlChannel` ABC.** D4 would have, but is
  deferred.
- **Schema changes.** D2 and D3 read existing `sessions` and
  `memory_session_messages` tables; no schema migrations.
- **`modexbot` facade.** The `modex_agent/cli/modexbot/` facade's
  known divergence from ADR-0022 D5 (it writes `pending.jsonl` directly
  instead of delegating to `modexctl`) is a pre-existing issue, not in
  scope for this spec.

## Further Notes

### Domain vocabulary

This spec uses terms defined in the repo's domain glossaries:
- **invocation_id** (subagent): the 8-byte hex prefix portion of a
  subagent's `session_id`. Full subagent session_id is
  `{invocation_id}.{agent_name}`. Subagent-exclusive — peer-normal
  communication has no invocation_id concept. See
  `docs/design/modexctl-enhancement/glossary.md`.
- **quadrant**: the four `send` output templates, indexed by
  `MODEX_COMM_KIND` × target kind. See glossary.
- **VO (View Object) whitelist**: the fixed 8-field set `history`
  exposes, filtering out internal markers. See glossary.
- **true history**: a history query that includes soft-deleted messages
  (`state='soft_deleted'`). Contrasts with `MessageStore.load_messages`
  (the LLM-context path) which filters to `state IN ('normal',
  'pinned')`. See glossary.
- **scope_key (subagent history)**: `RecordScope(session_id=...).canonical()`
  — yields `{"session_id": "<invocation_id>.<agent>"}`. See glossary.
- **not-found race**: the TOCTOU window in `send --invocation-id X`
  between the session-existence check and the bot's session
  registration. Accepted by design. See glossary.

### ADR relationships

- **Builds on**: ADR-0022 (modexctl + external coding agent
  integration), ADR-0019 (peer prefix-reuse), ADR-0023 (SQLite
  persistence), ADR-0028 (RecordScope canonical), ADR-0029 (epoch-ms
  timestamps), ADR-0030 (ColumnProjection / `_assemble_message`),
  ADR-0015 (inbox).
- **Does not modify**: ADR-0022's topology (external coding agents
  remain NORMAL main agents of their own pools), ADR-0019's
  prefix-reuse rule, the `ControlChannel` ABC (D4 is deferred).
- **Future**: a follow-up ADR for cancellation (D4) will need to extend
  `ControlChannel` (option 1a) or add an inbox-routed cancel envelope
  (option 1b).

### Implementation order suggestion

The four decisions have minimal interdependency and could be
implemented in any order. A natural order:

1. **D1** (disable rich rendering) — smallest, foundational; the
   `--help` output shape affects how agents discover D2/D3 commands.
2. **D2** (`send` enhancement) — largest single piece; depends on
   `sessions` table query + quadrant output formatter.
3. **D3** (`history` subcommand) — independent of D2; depends on
   `memory_session_messages` query + VO filter + JSON Lines
   serialization.
4. **D4** (cancellation) — deferred; no implementation work in this
   spec.

### Acceptance criteria

The acceptance criteria in `PRD.md` (under "Acceptance criteria")
apply verbatim. They are duplicated here for self-containedness:

#### D1
- `modexctl --help` output contains no box-drawing characters.
- `modexctl send --invalid-arg` exception trace is plain text.
- Existing `send` and `agents` commands still function (modulo D2.2
  output change for `send`).

#### D2
- `send --to X --content Y` (no `--invocation-id`,
  `MODEX_COMM_KIND=subagent`) → quadrant ② or ③ with `status:
  new_task`.
- `send --to X --content Y --invocation-id foo123` (session exists)
  → `status: resumed`, `invocation_id: foo123`.
- `send --to X --content Y --invocation-id foo123` (session NOT
  exists) → mint new uuid, `status: new_task (provided 'foo123' not
  found, created new)`, `invocation_id:` shows new uuid.
- `send --to X --content Y` (`MODEX_COMM_KIND=normal`) → quadrant ①
  (no session_id, no invocation_id).
- subagent → parent reply → quadrant ④.
- No output template mentions recipient's reply mechanism.
- `--invocation-id ""` ≡ omitting the flag.
- `--invocation-id` silently ignored when `MODEX_COMM_KIND=normal`.

#### D3
- `history --agent researcher --invocation-id abc12345` → up to 3
  JSON Lines, newest first.
- Each line `json.loads`-parseable.
- Each line contains only `_HISTORY_VO_FIELDS`.
- `--limit 5` → up to 5. `--limit 100` → clamped to 10. `--limit 0` /
  `-1` → usage error.
- Soft-deleted messages included.
- Missing `state.db` / empty table → stderr "No history found",
  exit 0.
- `MODEX_COMM_KIND != subagent` → `history` not registered.
- `content` with literal `\n` → serialized as `\\n`.

#### D4
- ADR-0035 D4 contains full requirement and background.
- No `cancel` subcommand registered.
