# Glossary — modexctl enhancement

Terms introduced or sharpened during the grilling session for ADR-0035.
Existing terms (Pool, Workspace, Session, InvocationId, etc.) are
defined in the root `CONTEXT.md` and are not repeated here.

## Terms

### invocation_id (subagent)

The 8-byte hex prefix portion of a subagent's `session_id`. The full
subagent session_id is `{invocation_id}.{agent_name}` (e.g.
`f3a9c1d2.researcher`). The invocation_id is:

- **Minted** by the framework (`SubagentDispatchStrategy.normalize_invocation_id`)
  when the caller omits it — a fresh `uuid4().hex[:8]`. Each mint
  creates a new subagent session.
- **Reused** when the caller passes a non-empty value — the subagent
  session `{value}.{agent_name}` is resumed (if it exists).
- **Subagent-exclusive**: peer-normal communication (ADR-0019) does not
  have an invocation_id concept; the peer session is
  `{self_prefix}.{target_name}` and the caller has no visibility into
  it.

`modexctl send --invocation-id` (ADR-0035 D2.1) mirrors this exactly.
`modexctl history --invocation-id` (D3.1) requires it because history
is meaningless without identifying the exact subagent session.

### session existence check

A best-effort SQL query (`SELECT 1 FROM sessions WHERE session_id = ?`)
executed by `modexctl send --invocation-id X` to decide whether to
report `status: resumed` or `status: new_task (provided 'X' not found,
created new)`. The check accepts a TOCTOU race: a second `send
--invocation-id X` invoked before the bot has registered session X from
the first send will mint a new uuid. The contract places responsibility
on the caller: "only pass `--invocation-id` for sessions you have
received a `<replied>` for."

### parent_session_id mismatch

A situation where `--invocation-id X` resolves to a session whose
`parent_session_id` (STORED generated column on the `sessions` table,
extracted from `scope_key`) does not equal `MODEX_SESSION_ID` (the
current caller's session_id). In a correctly wired single-main-per-pool
topology this cannot legitimately occur. ADR-0035 explicitly does NOT
deepen this into a dedicated error path — it is treated identically to
"session not found" (mint fresh uuid, report "not found, created new").

### quadrant (send output)

The four output templates `modexctl send` differentiates between,
indexed by `MODEX_COMM_KIND` and target agent kind:

| # | comm_kind | target kind | Includes |
|---|---|---|---|
| ① | `normal` | NATIVE or EXTERNAL | to, async-wait hint (no session_id, no invocation_id) |
| ② | `subagent` | NATIVE | invocation_id, session_id, status, output_path, trace_dir |
| ③ | `subagent` | EXTERNAL | invocation_id, session_id, status (no output_path/trace_dir) |
| ④ | subagent → parent | (parent) | parent session_id, "parent will continue" |

### VO (View Object) whitelist

The fixed set of fields `modexctl history` exposes to the agent,
filtering out internal markers:

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
`content_format`, `reasoning_content`. The whitelist is a single
frozenset — adding or removing a field is a one-line change with no
model surgery. Mirrors the Java VO pattern: externalize a stable,
minimal projection of an internal rich object.

### true history

A history query that includes soft-deleted messages
(`state='soft_deleted'` in the SQLite `memory_session_messages` table).
This contrasts with `MessageStore.load_messages` (the LLM-context path)
which filters to `state IN ('normal', 'pinned')`. The "true history"
semantics is the requirement that `modexctl history` show what actually
happened, including messages that were compressed out by
`prune_messages` / `retain_messages`. The `state` column is read to
attach the `_deleted` marker during `_assemble_message`, then stripped
by the VO filter — the agent does not see the marker, but the message
is included.

### scope_key (subagent history)

The `RecordScope.canonical()` JSON string used as the `WHERE scope_key =
?` predicate in `modexctl history`'s SQL query. For a subagent session
`{invocation_id}.{agent}`, the canonical form is exactly:

```json
{"session_id": "<invocation_id>.<agent>"}
```

This works because `SessionScope.extract()` only populates
`session_id`, and `RecordScope.canonical()` uses
`model_dump(exclude_none=True)` — the bot's `BotRecordScope.pool` field
is `None` at the framework base layer (ADR-0028) and is dropped. No
MemorySystem assembly, no scope resolver, no pool knowledge is
required.

### not-found race

The TOCTOU window in `modexctl send --invocation-id X`: between the
session-existence check and the bot's actual dispatch processing, the
bot may not yet have registered session X. A second `send
--invocation-id X` invoked in this window will see "not found" and mint
a new uuid. Accepted by design; documented in the contract.

### cancel-turn command (deferred)

A `ControlCommand(type=CANCEL_TURN, scope=ControlScope(session_id,
agent_id, turn_id))` delivered via `ControlChannel`. The only production
implementation is `InMemoryControlChannel` — process-internal, no
cross-process `deliver()` analog. ADR-0035 D4 records the requirement
for a `modexctl cancel` subcommand and the three pieces of
infrastructure needed (cross-process write surface, ReAct checkpoint
semantics, `SubagentAutoSendHook` integration) but does NOT implement
it.

## Cross-references

- **ADR-0022** — modexctl origin, `InboxMQ.deliver()` cross-process
  pattern (the model a future cancellation implementation would
  mirror).
- **ADR-0019** — peer prefix-reuse rule (determines the peer-normal
  target session_id that `modexctl send` does not expose).
- **ADR-0023** — hybrid persistence (SQLite is the bot's default;
  modexctl history is SQLite-only as a consequence).
- **ADR-0028** — RecordScope base/subclass split (why
  `BotRecordScope.pool=None` produces identical canonical JSON to base
  `RecordScope`).
- **ADR-0029** — epoch-millisecond timestamps (the `created_at` column
  `modexctl history` orders by).
- **ADR-0030** — ColumnProjection (the `_assemble_message` helper
  `modexctl history` reuses).
