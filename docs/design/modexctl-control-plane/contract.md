# modexctl Control Plane — Interface Contract

> Status: draft, pending Oracle architecture review.
> Evidence base: three independent explore investigations (legacy CLI audit,
> send runtime trace, workspace ownership map, history contract derivation).

## 1. Overview

Two fixed POST endpoints on the shared Control Origin:

```
POST /api/control/send
POST /api/control/history
```

Both accept typed JSON bodies. Neither encodes identity, session, workspace, or
content into URL query strings. No discovery endpoint, auth token, or caller
handle is introduced.

## 2. Shared model: AgentSessionRef

A single Pydantic model carries the four core locator fields shared by both
operations. The outer request field is always named `caller`; its business
meaning differs per operation but the structure is identical:

- In `send`, `caller` identifies the **sending agent** — the one invoking
  `send_to_agent`.
- In `history`, `caller` identifies the **session being queried** — whose
  history is requested.

Using one neutral field name (`caller`) instead of operation-specific names
(`source`/`target`) keeps the shared model recognizable across endpoints and
avoids implying that the structure itself differs.

```python
class AgentSessionRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Path
    # Absolute workspace root path. Maps to the WebUI `ws` convention.
    # The bot canonicalizes it and resolves PoolWorkspaceResources.

    pool: str
    # The pool name that owns this session. The bot validates this against
    # its own PoolStore agent-to-pool map; mismatch is a 409.

    session_id: str
    # Complete session id, e.g. "conv123.orchestrator" or "abc12345.coder".
    # Never a bare invocation prefix or conversation prefix.

    agent_name: str
    # The agent that owns this session. The bot validates this against
    # SessionInfo parsed from session_id; mismatch is a 409.
```

**Why all four fields are required even though some are derivable:**

- `workspace` cannot be inferred from `session_id` (multi-live workspace).
- `pool` cannot be inferred from `session_id` alone (same agent name may exist
  in different pools within one workspace).
- `agent_name` is derivable from `session_id` via `SessionInfo.from_str()`, but
  requiring it enables an explicit consistency check at no extra caller cost.
- `session_id` is the primary identity; the other three pin its context.

The bot treats client-supplied values as **claims to validate**, not as
authority. If `agent_name` does not match `SessionInfo.from_str(session_id)`, or
`pool` does not match the `PoolStore` agent-to-pool map, the bot returns `409`.

## 3. Send contract

### 3.1 Request

```python
class SendRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: AgentSessionRef
    # The sending agent's session locator.
    # Named `caller` to match the history endpoint's field — the structure
    # is identical; only the business meaning differs (sender vs queried).

    comm_kind: AgentCommKind
    # NORMAL or SUBAGENT. Required for topology policy and strategy selection.
    # SessionInfo does not independently encode this field.

    parent_session_id: str | None = None
    # SUBAGENT must supply this (its parent's full session id).
    # NORMAL must supply None.
    # The bot validates against SessionRegistry; if the session is registered
    # with a different parent, the bot uses the registered parent and does not
    # reparent.

    target_agent: str
    # Name of the target agent. Resolved from the source pool's live
    # CommunicationTargetStore, not from any client-supplied snapshot.

    content: str
    # Message body. Normalized by the CLI before sending.

    invocation_id: str | None = None
    # Subagent task continuation id. Only meaningful for NORMAL→SUBAGENT
    # dispatch. Peer and parent-reply strategies ignore it.
```

**JSON shape:**

```json
{
  "caller": {
    "workspace": "F:\\projects\\my-bot",
    "pool": "coder",
    "session_id": "conv123.orchestrator",
    "agent_name": "orchestrator"
  },
  "comm_kind": "normal",
  "parent_session_id": null,
  "target_agent": "coder",
  "content": "Please review the auth module.",
  "invocation_id": null
}
```

### 3.2 Response

```python
class DispatchOutcome(StrEnum):
    NEW_TASK = "new_task"
    # No invocation_id was requested (or was empty/None).
    # A fresh invocation was minted.

    RESUMED = "resumed"
    # The requested invocation_id matched an existing target session.
    # The session was continued.

    REQUESTED_INVOCATION_NOT_FOUND = "requested_invocation_not_found"
    # The requested invocation_id did not match an existing target session.
    # A different fresh invocation was minted.
    # requested_invocation_id carries the original value.

    NOT_APPLICABLE = "not_applicable"
    # Peer send or parent reply — invocation continuation does not apply.


class SendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    target_agent: str
    target_kind: AgentCommKind
    # NORMAL or SUBAGENT — the target's topology kind.

    session_id: str
    # The target session id constructed by the bot.
    # Subagent dispatch: "{invocation_id}.{target_agent}".
    # Parent reply: the parent's session_id.
    # Peer send: "{sender_prefix}.{target_agent}".

    invocation_id: str | None = None
    # Effective invocation id. Present for subagent dispatch; None for
    # peer and parent reply.

    dispatch_outcome: DispatchOutcome
    # Closed enum covering all strategy paths.

    requested_invocation_id: str | None = None
    # Original client-supplied invocation_id, present only when
    # dispatch_outcome == REQUESTED_INVOCATION_NOT_FOUND.
    # The CLI uses this to render the "provided 'X' not found, created new"
    # guidance.

    is_peer_send: bool = False
    # True for cross-pool peer delivery.

    is_external_target: bool = False
    # True when the target's execution_strategy is EXTERNAL.
    # Determines whether output_path/trace_dir apply.

    output_path: Path | None = None
    # Predicted OUTPUT.md path for native subagent dispatch. None for
    # external, peer, and parent reply.

    trace_dir: Path | None = None
    # Predicted trace directory for native subagent dispatch.
```

**Success response:** `200 OK` with `SendResult` JSON body.

### 3.3 Internal implementation flow

```
1. Parse and validate SendRequest (Pydantic).

2. Resolve workspace
   caller.workspace → WorkspaceResolver.resolve() → (WorkspaceContext, PoolWorkspaceResources)
   Extract resources as the second tuple element.

3. Locate caller's pool
   resources.pools[caller.pool] → PoolInstance
   If pool not found: 404.

4. Recover caller session context
   a. SessionInfo.from_str(caller.session_id) → SessionInfo
      Validate agent_name matches caller.agent_name. Mismatch: 409.
   b. SessionRegistry.get(caller.session_id) → registered parent
      If registered parent exists and differs from parent_session_id:
      use registered parent (SessionRegistry refuses to reparent).
      If not registered and comm_kind==SUBAGENT: validate parent_session_id
      is non-empty. Missing: 422.

5. Construct AgentContext (minimal, for communication service)
   - session: SessionInfo
   - comm_kind: caller.comm_kind (from request, not SessionInfo)
   - parent_session_id: resolved parent

6. Resolve target
   pool_instance.target_store.get(target_agent) → CommunicationTarget
   If not found: 404.
   If target_agent == caller.agent_name: 422 (self-send rejected).

7. Invocation existence check (subagent dispatch only)
   If CommunicationTarget.kind == SUBAGENT and invocation_id is non-empty:
     Check SessionRegistry or SessionStore for
     "{invocation_id}.{target_agent}".
     If exists: pass invocation_id to service as continuation.
     If not exists: mint new uuid4().hex[:8], record
       dispatch_outcome = REQUESTED_INVOCATION_NOT_FOUND,
       requested_invocation_id = original.
   If invocation_id is empty or None:
     mint new, dispatch_outcome = NEW_TASK.
   If CommunicationTarget is peer or parent reply:
     pass invocation_id=None, dispatch_outcome = NOT_APPLICABLE.

8. Execute send
   Call AgentCommunicationService._send() (structured result) or a new
   public structured-send method. Do NOT call send_async() (returns
   formatted text).

9. Map AgentSendResult → SendResult
   - is_external_target: from PoolSpec.execution_strategy
   - output_path/trace_dir: from AgentSendResult (already populated by
     the native strategy's _build_native_result). Do NOT re-derive from
     WorkspacePathResolver — it does not expose output_path()/trace_dir().
   - is_peer_send: from AgentSendResult.is_peer_send
   - dispatch_outcome: from step 7

10. Return 200 + SendResult.
```

## 4. History contract

### 4.1 Request

```python
class HistoryRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: AgentSessionRef
    # The session to query. Named `caller` to match the send endpoint —
    # the structure is identical; here it identifies the queried session
    # rather than the sending agent. The CLI constructs the full session_id
    # from --invocation-id and --agent before sending.

    limit: int = Field(default=3, ge=1, le=10)
    # Server-authoritative bound. Default 3, min 1, max 10.
    # The CLI clamps values >10 before sending and rejects <=0 as usage
    # error, preserving the existing CLI Compatibility Surface.
```

**JSON shape:**

```json
{
  "caller": {
    "workspace": "F:\\projects\\my-bot",
    "pool": "coder",
    "session_id": "abc12345.coder",
    "agent_name": "coder"
  },
  "limit": 3
}
```

### 4.2 Response

```python
class HistorySource(StrEnum):
    MESSAGE_STORE = "message_store"
    # Native ReAct sessions — raw MessageStore records.

    OBSERVABLE_TRANSCRIPT = "observable_transcript"
    # External coding sessions — materialized canonical transcript events.


class HistoryMessage(BaseModel):
    """Server Projection — eight-field whitelist with optional omission."""
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    # "user", "assistant", "tool", "system"

    content: str | list[dict[str, Any]] | None = None
    # str for text; list for multimodal ContentPart. None when the source
    # has no content for this record. Omitted from JSON when None
    # (exclude_none=True at serialization).

    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    name: str | None = None
    created_at: str | None = None
    # ISO 8601 string at the API surface (round-trips as int ms in SQLite).
    message_id: str | None = None
    # Present for MessageStore records. Absent for transcript-derived records.


class HistoryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: HistorySource
    session_id: str
    agent_name: str
    pool: str
    execution_strategy: str
    # "react" or "external" — lets the client distinguish without
    # conflating transcript history with provider memory.

    items: list[HistoryMessage]
    # Newest-first. At most `limit` logical records.
    # Empty list is a valid 200 response for a known session with no records.

    effective_limit: int
    # The limit applied after clamping. Echoed for client verification.
```

**Success response:** `200 OK` with `HistoryResult` JSON body.

### 4.3 Internal implementation flow

```
1. Parse and validate HistoryRequest (Pydantic).
   limit is already constrained to 1..10 by Pydantic.

2. Resolve workspace
   caller.workspace → WorkspaceResolver.resolve() → (WorkspaceContext, PoolWorkspaceResources)
   Extract resources as the second tuple element.

3. Look up exact session
   resources.session_index_store.get(caller.session_id) → SessionInfo
   If not found: 404.
   Validate agent_name matches caller.agent_name. Mismatch: 409.
   Empty session_id: 400 invalid_request.

3a. Target authorization (D26)
   The caller may read:
   - its own session history (caller.session_id == caller's own session), or
   - a subagent session registered under the caller's session (verified via
     SessionRegistry parent-child relationship).
   Otherwise: 403 forbidden_target.

4. Resolve pool from configuration
   PoolStore agent-to-pool map:
     a. exact main-agent match
     b. exact subagent-template match
   Validate against caller.pool. Mismatch: 409.
   If not found in any pool: 404.

5. Read execution strategy
   PoolSpec.main.execution_strategy for main agent.
   SubagentSpec.execution_strategy for subagent.
   If missing: 422 (configuration error).

6. Select data source deterministically
   EXTERNAL → HistorySource.OBSERVABLE_TRANSCRIPT
   All native strategies → HistorySource.MESSAGE_STORE
   Do NOT probe both stores and pick the non-empty one.

7a. Native MessageStore path
   a. Construct scope:
      BotRecordScope(
          workspace_id=str(canonical_workspace),
          pool=pool,
          session_id=caller.session_id,
      )
      This matches the runtime write path
      (base_scope.merge(SessionScope.extract(context))).
      Do NOT use legacy compute_scope_key() — it omits workspace and pool.

   b. Resolve MessageStore from the pool's PoolData.
      resources.pool_data[pool].context_manager → memory_system
      → session-scoped MemoryContext → MemoryStoreBundle → MessageStore

   c. Call load_all_messages()
      Includes soft-deleted records (true history).

   d. Project to HistoryMessage (Server Projection):
      - Retain original sequence number as tie-breaker.
      - Apply eight-field whitelist.
      - Serialize with exclude_none=True.

   e. Sort by (created_at, sequence) descending.
   f. Apply limit.

7b. External transcript path
   a. Load complete transcript:
      resources.workspace_transcript_store.load(caller.session_id)
      Do NOT use load_sessions_by_prefix() — that fans in sibling sessions.

   b. Materialize complete event list:
      _materialize_events(events)
      - Group by turn_id.
      - Coalesce text/reasoning by part_id.
      - Pair tool calls and results by call_id.
      - Retain user messages as independent logical entries.

   c. Project to HistoryMessage (Server Projection):
      - user event → role=user, content=text, created_at=event timestamp.
      - assistant text → role=assistant, content=coalesced text.
      - tool call → role=assistant, tool_calls=[...].
      - tool result → role=tool, tool_call_id=..., tool_name=..., content=...
      - Omit message_id (transcript has none).
      - Omit fields the source cannot supply (exclude_none=True).
      - Do NOT fabricate latency_ms, message_id, or tool_call_id.
      - Discard blocks with no representable CLI history record
        (e.g., incomplete tool event without a stable block).

   d. Merge user messages and materialized assistant turns.
   e. Sort by (timestamp, ordinal) descending.
   f. Apply limit to logical records, NOT raw events.

8. Return 200 + HistoryResult.
    Empty items list is a valid response for a known session with no records.
```

## 5. Error model

```python
class ControlError(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    # Machine-readable error code (see table below).

    message: str
    # Human-readable description. The CLI may augment with agent-facing
    # guidance; the bot does not emit coaching text.
```

| HTTP | Code | When |
|------|------|------|
| 400 | `validation_error` | Pydantic validation failure, missing required field, malformed JSON |
| 404 | `workspace_not_found` | Workspace path cannot be resolved or materialized |
| 404 | `pool_not_found` | `caller.pool` not in `resources.pools` |
| 404 | `session_not_found` | `session_id` not in SessionIndexStore |
| 404 | `target_agent_not_found` | `target_agent` not in CommunicationTargetStore |
| 403 | `forbidden_target` | History caller is not the session owner and not the parent of a registered subagent (D26) |
| 400 | `invalid_request` | Empty `session_id` in history request |
| 409 | `agent_name_mismatch` | `agent_name` does not match `SessionInfo.from_str(session_id)` |
| 409 | `pool_mismatch` | `pool` does not match PoolStore agent-to-pool map |
| 422 | `self_send_rejected` | `target_agent` equals caller `agent_name` |
| 422 | `topology_error` | TopologyPolicy.check() rejects the send (e.g., subagent→non-parent) |
| 422 | `missing_parent_session` | `comm_kind=subagent` but no parent_session_id and not registered |
| 422 | `execution_strategy_missing` | PoolSpec has no execution_strategy for the agent |
| 500 | `internal_error` | Unexpected failure during store read, materialization, or delivery |

All errors return the `ControlError` JSON body. The CLI maps all non-2xx
responses to exit code `2` (operation error), except 400-class validation issues
that originate from CLI-side validation which exit `1`.

## 6. Package structure

```
examples/bot_project/bot/control/
├── __init__.py          # Public exports
├── models.py            # AgentSessionRef, SendRequest, SendResult,
│                        # DispatchOutcome, HistoryRequest, HistoryResult,
│                        # HistoryMessage, HistorySource, ControlError
├── facade.py            # BotControlFacade — transport-independent interface
├── send.py              # send application logic (steps 2-9 of §3.3)
├── history.py           # history application logic (steps 2-8 of §4.3)
└── routes.py            # aiohttp route adapters → BotControlFacade

examples/bot_project/bot/workspace/
└── request_resolver.py  # Extracted from WebUIServer._ws_root_of etc.
                         # Shared ws parser used by WebUI and control routes.
```

**BotControlFacade:**

```python
class BotControlFacade:
    """Transport-independent bot control application interface."""

    def __init__(
        self,
        workspace_resolver: WorkspaceResolver,
        # Other dependencies injected from BotService/WebUIService
    ) -> None: ...

    async def send(self, request: SendRequest) -> SendResult: ...

    async def history(self, request: HistoryRequest) -> HistoryResult: ...
```

HTTP route handlers in `routes.py` are thin: parse JSON → Pydantic model →
call facade → serialize result. No business logic in the route layer.

## 7. CLI adaptation

### 7.1 Environment → request mapping

| Environment variable | Send field | History field |
|---|---|---|
| `MODEX_WORKSPACE_ROOT` | `caller.workspace` | `caller.workspace` |
| `MODEX_SESSION_ID` | `caller.session_id` | — (CLI builds caller.session_id) |
| `MODEX_AGENT_NAME` | `caller.agent_name` | — (CLI uses --agent) |
| `MODEX_COMM_KIND` | `comm_kind` | — (gate only, not sent) |
| `MODEX_PARENT_SESSION_ID` | `parent_session_id` | — |
| `MODEX_AGENT_POOL_MAP` | resolve `caller.pool` | resolve `caller.pool` |
| `MODEX_CONTROL_ORIGIN` | endpoint origin | endpoint origin |

`MODEX_TARGETS` and `MODEX_AGENT_POOL_MAP` are **not** sent in the request body.
`MODEX_AGENT_POOL_MAP` is used locally by the CLI to look up a single pool name
for the caller's agent; the bot validates this against its own PoolStore.

### 7.2 Send CLI flow

```
1. Build ModexCtlContext from environment (D25). Validates MODEX_* vars,
   provides smart defaults per normal/subagent mode.
2. Read CLI args: positional message (primary, D29), --to (defaults to
   parent for subagents, D5), --content/--content-file/--stdin (fallbacks),
   --invocation-id.
3. Resolve caller.pool from MODEX_AGENT_POOL_MAP[MODEX_AGENT_NAME].
4. Construct AgentSessionRef (as `caller`) + SendRequest.
5. POST to {MODEX_CONTROL_ORIGIN}/api/control/send.
6. On 200: parse SendResult.
   - Map dispatch_outcome to status text:
     NEW_TASK → "status: new_task"
     RESUMED → "status: resumed"
     REQUESTED_INVOCATION_NOT_FOUND →
       "status: new_task (provided '{requested_invocation_id}' not found, created new)"
     NOT_APPLICABLE → peer/parent reply formatting
   - Map is_external_target to external/native formatting.
   - Output to stdout (cleaned of internal terms, D5). Exit 0.
7. On non-2xx: parse ControlError, write to stderr. Exit 2.
8. On connection failure/timeout: write to stderr. Exit 2.
```

### 7.3 History CLI flow

```
1. Build ModexCtlContext from environment (D25).
2. Read CLI args: --agent (optional for self-history, D19),
   --invocation-id (optional for self-history, D19), --limit.
3. If --agent and --invocation-id both provided:
   Construct session_id as "{invocation_id}.{agent}".
   Else (self-history): use caller's own session_id from context.
4. Resolve caller.pool from MODEX_AGENT_POOL_MAP (caller's agent for
   self-history, or --agent for subagent history).
5. Clamp limit: if <=0, exit 1 (usage error). If >10, clamp to 10.
6. Construct AgentSessionRef (as `caller`) + HistoryRequest.
7. POST to {MODEX_CONTROL_ORIGIN}/api/control/history.
8. On 200: parse HistoryResult.
   - Apply Client Output Projection (independent eight-field whitelist).
   - Serialize each item as one JSONL line (json.dumps, ensure_ascii=False).
   - Output to stdout. Exit 0.
9. On 403 forbidden_target: write error to stderr. Exit 2.
10. On other non-2xx: parse ControlError, write to stderr. Exit 2.
11. On connection failure/timeout: write to stderr. Exit 2.
```

> **Phase 4 changes (D5, D19, D25, D26, D29):** The send flow uses
> `ModexCtlContext` for env interpretation, positional message args as
> the primary input, and `--to` defaulting to parent for subagents.
> The history flow is ungated from `MODEX_COMM_KIND=subagent` (all
> agents can read own history), `--agent`/`--invocation-id` are optional
> for self-history, and target authorization (403 forbidden_target) is
> enforced by the bot.

## 8. Self-check

### 8.1 Consistency with confirmed decisions

| Decision | Contract alignment |
|---|---|
| D1 new bot-owned client | ✅ Contract lives in `bot/control/` |
| D2 no runtime fallback | ✅ CLI has no legacy/SQLite path |
| D3 package before client | ✅ `BotControlFacade` defined before CLI |
| D4 validate bootstrap, defer auth | ✅ CLI validates env; no auth in contract |
| D5 CLI surface redesigned for ergonomics | ✅ Kind labels, cleaned output, positional args, ModexCtlContext (Phase 4) |
| D6 control origin only | ✅ `MODEX_CONTROL_ORIGIN` carries origin only |
| D7 path to full decomposition | ✅ `BotControlFacade` + `request_resolver` pattern |
| D8 agents local with kind labels | ✅ `agents` shows kind labels + behavioral docs; subagent view shows parent only (Phase 4) |
| D9 thin routes over existing capabilities | ✅ Reuses AgentCommunicationService, MessageStore, TranscriptStore |
| D10 fixed POST, server-owned limit | ✅ `POST /api/control/{send,history}`; `limit: ge=1, le=10` |
| D11 independent projections | ✅ `HistoryMessage` (server) vs CLI eight-field (client) |
| D12 Dispatch Outcome enum | ✅ `DispatchOutcome` with four values |
| D13 outcome in bot control, not framework | ✅ Existence check in `send.py`, not in AgentCommunicationService |
| D14 no topology env in HTTP | ✅ `MODEX_TARGETS`/`MODEX_AGENT_POOL_MAP` not in request |
| D15 Python implementation | ✅ All models are Pydantic |
| D16 public/private command dirs | ✅ Not affected by contract |
| D17 exit codes unchanged | ✅ 0/1/2 mapping preserved |
| D18 no retries, short timeout | ✅ Not in contract but CLI implements |
| D19 history ungated, target auth | ✅ All agents read own history; target authorization enforces 403 forbidden_target (Phase 4) |
| D20 materialize before limit | ✅ Step 7b loads full transcript, materializes, then limits |
| D21 source fidelity | ✅ Missing fields omitted, not fabricated |
| D22 workspace required | ✅ `AgentSessionRef.workspace` is mandatory |
| D23 shared workspace resolver | ✅ `request_resolver.py` extracted from WebUIServer |

### 8.2 Send contract checks

- ✅ `caller.session_id` is in the request body (not a header, not omitted).
- ✅ `comm_kind` and `parent_session_id` are explicit send request fields.
- ✅ `target_agent`, `content`, `invocation_id` mirror `send_to_agent`.
- ✅ No `target_session_id` in request — bot constructs it.
- ✅ No `MODEX_TARGETS` or pool map in request body.
- ✅ `SendResult` carries structured facts; CLI generates ack text.
- ✅ `DispatchOutcome` covers all four strategy paths.
- ✅ `requested_invocation_id` only present when not-found.
- ✅ Invocation existence check is in bot control application (D13).
- ✅ `AgentCommunicationService._send()` is called, not `send_async()`.
- ✅ `output_path`/`trace_dir` only for native subagent dispatch.
- ✅ `is_external_target` derived from PoolSpec, not from env.

### 8.3 History contract checks

- ✅ `caller.session_id` is complete (CLI constructs from invocation+agent).
- ✅ No `invocation_id` or `agent` as separate endpoint fields.
- ✅ `limit` constrained by Pydantic `ge=1, le=10` at the bot boundary.
- ✅ CLI clamps >10 and rejects <=0 before sending.
- ✅ `HistoryMessage` fields match the eight-field whitelist.
- ✅ Missing fields omitted via `exclude_none=True`.
- ✅ `message_id` absent for transcript-derived records.
- ✅ No `latency_ms` or fabricated fields.
- ✅ Scope uses `BotRecordScope(workspace, pool, session_id)`, not legacy
     `compute_scope_key()`.
- ✅ Transcript uses exact `load(session_id)`, not prefix fan-in.
- ✅ Materialization happens before limiting.
- ✅ Source selection is configuration-driven, not probe-driven.
- ✅ Empty items is 200, not 404.
- ✅ `HistorySource` enum distinguishes message_store vs transcript.

### 8.4 Error model checks

- ✅ All non-2xx return `ControlError` with `code` and `message`.
- ✅ 409 used for consistency violations (agent_name/pool mismatch).
- ✅ 422 used for semantic rejections (self-send, topology, missing parent).
- ✅ CLI maps all non-2xx to exit code 2.
- ✅ CLI-side validation errors (missing env, bad args) exit 1 before HTTP.

### 8.5 Open items pending Oracle review

1. Whether `AgentCommunicationService` needs a new public structured-send
   method, or whether `_send()` can be elevated to public.
2. Whether `SessionRegistry` is the correct existence-check source for
   invocation continuation, or whether `SessionStore` (persistent) should also
   be consulted for sessions that were materialized but evicted.
3. Whether the `request_resolver.py` extraction should return a structured
   `WorkspaceResolution` result or continue returning `Path`.
4. Whether `PoolData` access for history needs the same turn-pinning safety
   that inbound turns use, or whether read-only access is safe without pinning.

> Oracle review completed (bg_4aad941a). Issues 1-4 above remain as
> implementation decisions, not design blockers. Oracle found and we fixed:
> `session_index_store` naming, `output_path`/`trace_dir` sourcing from
> `AgentSendResult` (not WorkspacePathResolver), D24 `source`→`caller`
> naming, D10 clarification. No blocking issues remain.

## 9. Deployment integration — control origin injection and CLI discovery

> This section closes the gap between the HTTP contract (§3-§5) and the two
> real execution environments: local development and packaged Windows install.
> Without it, `MODEX_CONTROL_ORIGIN` is an undefined input.

### 9.1 The missing injection chain

The current codebase constructs 9 `MODEX_*` env vars in
`ExternalEnvBuilder.build_modex_vars()` (env_builder.py:59-78) and injects
them via two paths:

- **External agents**: `ExternalEnvBuilder.build(spec, base_env)` at spawn time.
- **Native agents**: `NativeEnvInjectionHook.before_turn()` sets the
  `_modex_env` contextvar, which `SubprocessExecutor`/`CommandTool` read when
  spawning subprocesses.

Neither path currently sets `MODEX_CONTROL_ORIGIN`. The bot knows its HTTP
address at startup (from `bot_config.yml` → `webui.host` + `webui.port`), but
this value does not reach `ExternalEnvSpec` or the env injection hook.

### 9.2 Changes to ExternalEnvSpec

Add one field to `ExternalEnvSpec` (types.py):

```python
class ExternalEnvSpec(BaseModel):
    # ... existing fields ...

    control_origin: str = Field(
        description=(
            "The bot HTTP listener origin (scheme + host + port) injected as "
            "MODEX_CONTROL_ORIGIN so the Control Client can locate the bot "
            "control endpoints. Example: 'http://127.0.0.1:21800'."
        ),
    )
```

This field is populated by the bot at spec construction time (pool_builder or
wiring), not from any config file the CLI reads.

### 9.2a Changes to AgentMaterializeDeps (Phase 4, D6)

`AgentMaterializeDeps` gains a matching `control_origin` field so native
subagents (materialized via the template path) also receive
`MODEX_CONTROL_ORIGIN` in their `ExternalEnvSpec`. Without this, native
subagent `modexctl send` failed with an empty `MODEX_CONTROL_ORIGIN`.

The field is set from `build_control_origin` at boot and passed to the
subagent `ExternalEnvSpec` in the template construction path.

### 9.3 Changes to ExternalEnvBuilder

`build_modex_vars()` (env_builder.py:59-78) adds one line:

```python
modex["MODEX_CONTROL_ORIGIN"] = spec.control_origin
```

Both `build()` (external spawn) and `NativeEnvInjectionHook.before_turn()`
(native contextvar) call `build_modex_vars()`, so both paths get the value
from the single extraction point — consistent with ADR-0022 D6.

### 9.4 Bot-side origin resolution

At pool/wiring construction time, the bot reads its HTTP origin:

```text
bot_config.yml → webui.host + webui.port
→ construct origin string: f"http://{host}:{port}"
→ normalize: if host == "0.0.0.0", use "127.0.0.1" for injection
→ pass to ExternalEnvSpec(control_origin=origin)
```

The injection always uses a loopback address, even when the bot listens on
`0.0.0.0`. The CLI's `MODEX_CONTROL_ORIGIN` validation (§9.5) rejects
non-loopback origins.

### 9.5 CLI-side validation

The Control Client validates `MODEX_CONTROL_ORIGIN` at startup:

- Must be present and non-empty.
- Must parse as a URL with `http` or `https` scheme.
- Host must be loopback: `127.0.0.1`, `localhost`, or `[::1]`.
- Port must be present and in 1..65535.
- No path, query, or fragment component.
- Failure → exit code 1 (usage/environment error, D17).

### 9.6 Local development scenario

```text
Developer runs:  python -m modexbot start  (or debug_main.py)
                 ↓
BotService.initialize()
  → reads bot_config.yml → webui.port=21800, webui.host="0.0.0.0"
  → WebUIServer starts on 0.0.0.0:21800
  → pool_builder constructs ExternalEnvSpec for each pool
    → control_origin = "http://127.0.0.1:21800"
  → ExternalEnvBuilder.build_modex_vars() includes MODEX_CONTROL_ORIGIN
  → NativeEnvInjectionHook sets _modex_env contextvar with MODEX_CONTROL_ORIGIN
                 ↓
External agent (Pi/OpenCode) spawned with MODEX_CONTROL_ORIGIN in env
  → modexctl invoked from agent's bash/terminal
  → reads MODEX_CONTROL_ORIGIN → POST to http://127.0.0.1:21800/api/control/*
                 ↓
Native agent (ReAct) runs with _modex_env contextvar
  → SubprocessExecutor reads _modex_env → inherits MODEX_CONTROL_ORIGIN
  → modexctl invoked from native agent's terminal tool
  → same path
```

**Console script in local dev:**

The new CLI is registered as a console script in
`examples/bot_project/pyproject.toml`:

```toml
[project.scripts]
modexctl = "bot.cli.modexctl:main"
```

After `uv pip install -e ".[dev,llm,storage,gateway]"`, the venv's
`Scripts/modexctl.exe` (Windows) or `bin/modexctl` (POSIX) points to the new
entry. The root `pyproject.toml`'s `modexctl = "modexctl.main:main"` entry is
removed during migration (D2).

`ExternalEnvSpec.modexctl_bin_dir` is updated to point to the resolved Public
Command Directory (D16), which in local dev is the venv's `Scripts/` or `bin/`.

### 9.7 Packaged Windows install scenario

```text
Installer (Inno Setup) runs:
  → prepare_bundled_bin.py stages rg.exe → <staging>/bin/windows/
  → postinstall.py runs on user's machine:
    1. create_pth_files() — .pth links to src/ and examples/bot_project/
    2. create_cli_shims() — creates launchers in <install>/python/Scripts/
       → modexbot.bat:  python.exe -m modexbot %*
       → modexctl.bat:   python.exe -m bot.cli.modexctl %*    ← CHANGED
    3. register_scripts_on_path() — registers python/Scripts on HKCU PATH
    4. verify_imports() — checks import bot, import modex_agent

Bot starts (Tauri shell or desktop shortcut):
  → reads bot_config.yml → port 21800
  → WebUIServer starts
  → pool_builder constructs ExternalEnvSpec
    → control_origin = "http://127.0.0.1:21800"
    → modexctl_bin_dir = <install>/python/Scripts  (Public Command Directory)
  → ExternalEnvBuilder.build() prepends modexctl_bin_dir to PATH
  → MODEX_CONTROL_ORIGIN injected into agent env

Agent invokes modexctl:
  → PATH finds <install>/python/Scripts/modexctl.bat
  → modexctl.bat → python.exe -m bot.cli.modexctl %*
  → reads MODEX_CONTROL_ORIGIN → POST to http://127.0.0.1:21800/api/control/*
```

### 9.8 postinstall.py changes

`create_cli_shims()` (postinstall.py:102-118) changes one line:

```python
# BEFORE:
"modexctl.bat": f'@echo off\r\n"{python_exe}" -c "from modexctl.main import main; main()" %*',

# AFTER:
"modexctl.bat": f'@echo off\r\n"{python_exe}" -m bot.cli.modexctl %*',
```

`verify_imports()` (postinstall.py:155-161) changes one check:

```python
# BEFORE:
"import modexctl; print('  modexctl OK')",

# AFTER:
"import bot.cli.modexctl; print('  bot.cli.modexctl OK')",
```

### 9.9 D16 commands/ directory — deferred

D16 proposed separating `<install>/commands/` (public) from
`<install>/bin/windows/` (private). The current packaging puts CLI shims in
`<install>/python/Scripts/` and registers that directory on PATH.

For the first implementation, the new CLI continues using
`<install>/python/Scripts/` as the Public Command Directory — this is where
postinstall.py already creates shims and registers PATH. The separate
`commands/` directory layout from D16 is a packaging refinement that can be
done later without changing the CLI contract or the env injection chain.

What changes now:

- `modexctl.bat` content points to the new module.
- `modexctl_bin_dir` in `ExternalEnvSpec` continues to point to the resolved
  Scripts directory (same as today).
- The `MODEXBOT_BIN_DIR` concept and `modexctl_bin_dir` field are not renamed
  in this phase; they are already functionally equivalent to the Public
  Command Directory for the packaged case.

### 9.10 What does NOT change

- `prepare_bundled_bin.py` — still stages only `rg.exe`; no CLI binaries.
- `build.bat` / `build_archive.py` / `prepare_python.py` — no changes needed;
  the new CLI is Python source, discovered via `.pth` files like all other
  bot code.
- `modexbot.iss` (Inno Setup) — no changes needed; it already includes
  `python/Scripts/` and runs `postinstall.py`.
- `bot_config.yml` — no new config keys; `webui.port`/`webui.host` already
  exist and are the single source of truth for the HTTP listener address.
- `install.bat` / `install.sh` — dev install continues to use
  `uv pip install -e .` which registers console scripts normally.

### 9.11 Self-check — deployment integration

| Concern | Local dev | Packaged Windows |
|---|---|---|
| `MODEX_CONTROL_ORIGIN` source | bot_config.yml → pool_builder → ExternalEnvSpec | same |
| `MODEX_CONTROL_ORIGIN` injection | ExternalEnvBuilder.build_modex_vars() + NativeEnvInjectionHook | same |
| CLI executable location | venv `Scripts/modexctl.exe` or `bin/modexctl` | `<install>/python/Scripts/modexctl.bat` |
| CLI module | `bot.cli.modexctl:main` | same |
| Console script registration | `examples/bot_project/pyproject.toml [project.scripts]` | postinstall.py `create_cli_shims()` |
| PATH for agent subprocesses | `modexctl_bin_dir` prepended by ExternalEnvBuilder | same |
| Bot HTTP address discovery | `MODEX_CONTROL_ORIGIN` env var | same |
| Config file read by CLI | none | none |
| Port scanning by CLI | no | no |
| Fallback to SQLite | no (D2) | no (D2) |

## 10. Legacy modexctl deprecation

> D2 established that the legacy `src/modexctl` source is retained as a
> reference implementation, not as a runtime fallback. This section specifies
> the concrete deprecation steps and the coupling that must be addressed.

### 10.1 Current coupling map

The legacy `src/modexctl` package is not a standalone leaf. Three categories
of code depend on it:

**A. Console script registration (root pyproject.toml:100):**
```toml
[project.scripts]
modexctl = "modexctl.main:main"
```
This registers the public `modexctl` command. The new CLI must own this name
(D2), so this entry must be removed from the root `pyproject.toml` and added
to `examples/bot_project/pyproject.toml`.

**B. Framework modexbot CLI facade (src/modex_agent/cli/modexbot/):**

`modexbot` is a framework-side CLI that delegates to `modexctl.main` internal
functions for routing, inbox-line construction, and file writing:

| File | Imports from `modexctl.main` |
|------|------------------------------|
| `src/modex_agent/cli/modexbot/main.py:39` | `_normalize_text`, `_parse_pool_map` |
| `src/modex_agent/cli/modexbot/routing.py:12-26` | `_build_inbox_line`, `_compute_target_session_id`, `_resolve_target_pool`, `_MalformedSessionIdError`, `_UnknownTargetError` |
| `src/modex_agent/cli/modexbot/writer.py:12` | `_write_line` |

These imports are of **private** functions (prefixed `_`). The `modexbot` CLI
is currently a thin facade that re-exports `modexctl`'s routing logic with
typed exception translation.

**C. Tests and packaging:**

| File | Import / reference |
|------|--------------------|
| `tests/unit/cli/modexctl/test_main.py` | `from modexctl.main import ...`, `from modexctl.quadrant import ...` |
| `tests/unit/cli/modexctl/test_parent_session_id_propagation.py` | `from modexctl.main import _PoolScopedRecordScope, build_app` |
| `tests/unit/cli/modexctl/test_sqlite_persistence_unification.py` | `from modexctl.main import _PoolScopedRecordScope` |
| `tests/unit/cli/modexctl/test_external_communication.py` | `from modexctl.main import build_app` |
| `tests/unit/cli/modexctl/test_cross_pool_peer_messaging.py` | `from modexctl.main import _PoolScopedRecordScope, build_app` |
| `examples/bot_project/tests/unit/service/test_subagent_external_builder.py:367` | `from modexctl.main import _parse_pool_map, _resolve_target_pool` |
| `examples/bot_project/packaging/windows/postinstall.py:111` | shim: `from modexctl.main import main` |
| `examples/bot_project/packaging/windows/postinstall.py:157` | verify: `import modexctl` |

### 10.2 Deprecation strategy

The legacy source is **retained but not installed as the public command**.
Specifically:

**Step 1 — Move console script ownership:**

```toml
# Root pyproject.toml — REMOVE this line:
# modexctl = "modexctl.main:main"

# examples/bot_project/pyproject.toml — ADD:
[project.scripts]
modexbot = "modexbot.cli:app"
modexctl = "bot.cli.modexctl:main"
```

After `uv pip install -e .` in the repo, the venv's `modexctl` entry points to
`bot.cli.modexctl:main`. The legacy `src/modexctl` source remains importable
(`from modexctl.main import ...` still works in tests) but is no longer the
installed command.

**Step 2 — Keep modexbot CLI functional during transition:**

The `modexbot` CLI (`src/modex_agent/cli/modexbot/`) is a framework-side
facade that currently delegates to `modexctl.main` private functions. During
the transition, these imports continue to work because `src/modexctl` source
is retained. The `modexbot` CLI is not modified in this phase.

A future decision will either:
- migrate `modexbot` to also use the new HTTP control plane (making it a
  second thin client), or
- retire `modexbot` entirely if the new `modexctl` subsumes its surface.

Neither is a prerequisite for the new CLI.

**Step 3 — Keep legacy tests functional:**

The 6 test files under `tests/unit/cli/modexctl/` and
`examples/bot_project/tests/` that import from `modexctl.main` continue to
work because the source is retained. These tests document the legacy behavior
contract and serve as the reference implementation's executable specification.

They are **not** the new CLI's tests. The new CLI gets its own test suite
under `examples/bot_project/tests/unit/cli/modexctl/` (or similar) that
verifies the HTTP-based behavior and CLI Compatibility Surface.

**Step 4 — Update packaging:**

`postinstall.py` changes (already specified in §9.8):
- `create_cli_shims()`: `modexctl.bat` points to `bot.cli.modexctl`
- `verify_imports()`: checks `import bot.cli.modexctl`

The legacy `import modexctl` check is removed from `verify_imports()`.

**Step 5 — Do not delete source:**

`src/modexctl/` and its `__main__.py`, `main.py`, `quadrant.py`, `history.py`
remain in the repository. They are not included in the root wheel's
`[project.scripts]` but remain importable for tests and reference. A
`# DEPRECATED` marker or module docstring is added to make the status visible.

### 10.3 What the deprecation does NOT do

- Does not delete `src/modexctl/` source.
- Does not modify `modexbot` CLI behavior.
- Does not break existing `from modexctl.main import ...` in tests.
- Does not introduce a compatibility shim or alias for the console script.
- Does not support running both old and new `modexctl` simultaneously.
- Does not keep `src/modexctl` in the root wheel's packages list beyond what
  test imports require (it stays in `packages = ["src/modex_agent", "src/modexctl"]`
  so tests can import it; the console script entry is removed).

### 10.4 Migration safety

| Risk | Mitigation |
|------|------------|
| Install order: root installs old `modexctl`, bot_project installs new | Root `pyproject.toml` removes the `modexctl` script entry. Only `examples/bot_project/pyproject.toml` registers it. |
| `modexbot` CLI breaks because `modexctl.main` internals change | `src/modexctl` source is not modified. `modexbot` imports continue to resolve. |
| Legacy tests fail after console script removal | Tests import from `modexctl.main` directly, not via the console script. They continue to pass. |
| Packaged install still has old `modexctl.bat` | `postinstall.py` is updated (§9.8). Old builds are not affected; new builds generate the correct shim. |
| Developer runs `modexctl` and gets old version | After `uv pip install -e .`, the venv entry points to `bot.cli.modexctl:main`. The root no longer registers a competing entry. |

## 11. Comprehensive design self-check

> This section verifies the entire contract (§1-§10) against all confirmed
> decisions (D1-D24), all user-confirmed constraints, and all real-world
> execution scenarios. Each check cites the evidence source.

### 11.1 Decision coverage (D1-D24)

| Decision | Status | Evidence |
|---|---|---|
| D1 new bot-owned client | ✅ | CLI module: `bot.cli.modexctl`; package: `bot/control/` |
| D2 no runtime fallback | ✅ | §10.2: legacy source retained but console script removed; §7: CLI has no SQLite/legacy path |
| D3 package before client | ✅ | §6: `BotControlFacade` defined; §7: CLI is thin adapter |
| D4 validate bootstrap, defer auth | ✅ | §7.1: env mapping; §9.5: origin validation; no auth subsystem |
| D5 CLI surface redesigned for ergonomics | ✅ | §7.2-7.3: kind labels, cleaned output, positional args, ModexCtlContext (Phase 4) |
| D6 control origin only | ✅ | §9.1-9.4: `MODEX_CONTROL_ORIGIN` injected via existing chain |
| D7 path to full decomposition | ✅ | §6: `BotControlFacade` + `request_resolver` extraction pattern |
| D8 agents local with kind labels | ✅ | `agents` shows kind labels + behavioral docs; subagent view shows parent only (Phase 4) |
| D9 thin routes over existing capabilities | ✅ | §3.3: `AgentCommunicationService._send()`; §4.3: `MessageStore`/`TranscriptStore` |
| D10 fixed POST, server-owned limit | ✅ | §3.1/4.1: `POST /api/control/{send,history}`; `limit: ge=1, le=10` |
| D11 independent projections | ✅ | §4.2: `HistoryMessage` (server) vs §7.3 step 8 (client eight-field) |
| D12 Dispatch Outcome enum | ✅ | §3.2: four values: `new_task`/`resumed`/`requested_invocation_not_found`/`not_applicable` |
| D13 outcome in bot control | ✅ | §3.3 step 7: existence check in control application, not framework service |
| D14 no topology env in HTTP | ✅ | §7.1: `MODEX_TARGETS`/`MODEX_AGENT_POOL_MAP` not in request |
| D15 Python implementation | ✅ | All models Pydantic; no Rust/Cargo |
| D16 public/private command dirs | ✅ | §9.9: first impl uses `python/Scripts/`; separate `commands/` deferred |
| D17 exit codes unchanged | ✅ | §5: 0/1/2; §7.2-7.3: HTTP failures → exit 2 |
| D18 no retries, short timeout | ✅ | Not in HTTP contract; CLI implements 1s connect / 10s total / no retry |
| D19 history ungated, target auth | ✅ | All agents read own history; target authorization enforces 403 forbidden_target (Phase 4) |
| D20 materialize before limit | ✅ | §4.3 step 7b: full load → materialize → project → sort → limit |
| D21 source fidelity | ✅ | §4.3 step 7b: omit unavailable fields; no fabrication |
| D22 workspace required | ✅ | `AgentSessionRef.workspace` mandatory; no workspace inference |
| D23 shared workspace resolver | ✅ | §6: `request_resolver.py` extracted from `WebUIServer._ws_root_of` |
| D24 unified AgentSessionRef | ✅ | §2: `caller: AgentSessionRef` in both send and history |

### 11.2 User-confirmed constraints

| Constraint | Status | Where addressed |
|---|---|---|
| New CLI in `examples/bot_project/`, not modify old | ✅ | §6: `bot/control/` + `bot/cli/modexctl`; §10: old source retained |
| Python implementation, not Rust | ✅ | D15; all models Pydantic |
| Environment validation required | ✅ | §7.1, §9.5 |
| No large params in URL | ✅ | §3.1, §4.1: POST JSON bodies |
| Env-only operations don't call bot | ✅ | `agents` is local-only (D8) |
| Bot `server.py` refactor first | ✅ | D7/D23: extract `request_resolver` + `BotControlFacade` before CLI |
| `caller` as unified field name | ✅ | §2: both endpoints use `caller: AgentSessionRef` |
| History `session_id` constructed by CLI | ✅ | §7.3 step 3: CLI builds `{invocation_id}.{agent}` |
| Send returns invocation_id + session_id | ✅ | §3.2: `SendResult.session_id`, `SendResult.invocation_id` |
| Dispatch Outcome as enum | ✅ | §3.2: `DispatchOutcome` StrEnum |
| No status/cancel commands | ✅ | D5: only `agents`/`send`/`history` preserved |
| Bot interface returns raw structures | ✅ | `SendResult`/`HistoryResult` are typed data, no coaching text |
| CLI keeps agent-facing guidance layer | ✅ | §7.2 step 6: CLI maps `dispatch_outcome` → status text |
| Two independent field filters (server + client) | ✅ | D11; §4.2 vs §7.3 step 8 |
| External transcript: materialize then limit | ✅ | D20; §4.3 step 7b |
| Missing response fields omitted, not fabricated | ✅ | D21; §4.3 step 7b |
| `workspace_root` must be passed (multi-workspace) | ✅ | D22; `AgentSessionRef.workspace` |
| Borrow WebUI workspace convention (`ws`) | ✅ | D23; `request_resolver.py` reuses `_ws_root_of` |
| `send` modeled after `send_to_agent` | ✅ | D24; `target_agent`/`content`/`invocation_id` |
| `history` modeled after SessionInfo | ✅ | D24; endpoint accepts `session_id`, not `invocation_id` |
| No auth/capability token in first phase | ✅ | D4; deferred hardening |
| Local dev and packaged install both work | ✅ | §9.6 (local dev), §9.7 (packaged) |
| Legacy modexctl deprecated, not deleted | ✅ | §10: source retained, console script moved |

### 11.3 Execution scenario coverage

| Scenario | Covered | Section |
|---|---|---|
| Local dev: `python -m modexbot start` → external agent → `modexctl send` | ✅ | §9.6 |
| Local dev: native ReAct agent → terminal tool → `modexctl send` | ✅ | §9.6 (NativeEnvInjectionHook path) |
| Local dev: `modexctl history` from subagent | ✅ | §7.3 + §9.6 |
| Local dev: `modexctl agents` (no bot needed) | ✅ | D8 (local snapshot) |
| Packaged Windows: Tauri shell → bot → external agent → `modexctl send` | ✅ | §9.7 |
| Packaged Windows: `modexctl.bat` discovery via PATH | ✅ | §9.7 + §9.8 |
| Packaged Windows: `postinstall.py` creates correct shim | ✅ | §9.8 |
| Legacy `modexbot` CLI continues working | ✅ | §10.2 step 2 |
| Legacy tests continue passing | ✅ | §10.2 step 3 |
| Old `modexctl` source not deleted | ✅ | §10.2 step 5 |
| Console script ownership transfers cleanly | ✅ | §10.2 step 1 |
| `MODEX_CONTROL_ORIGIN` reaches external agent env | ✅ | §9.2-9.3 |
| `MODEX_CONTROL_ORIGIN` reaches native agent contextvar | ✅ | §9.3 (NativeEnvInjectionHook) |
| CLI rejects non-loopback origin | ✅ | §9.5 |
| CLI rejects missing origin | ✅ | §9.5 (exit 1) |
| Bot unavailable → CLI fails (no fallback) | ✅ | D2, D17 (exit 2) |
| `send` timeout → no retry | ✅ | D18 |
| `history` on external agent → transcript source | ✅ | §4.3 step 7b |
| `history` on native agent → MessageStore source | ✅ | §4.3 step 7a |
| `history` empty session → 200 with `items: []` | ✅ | §4.3 step 8 |
| `send --invocation-id` not found → new task + guidance | ✅ | §3.3 step 7, §7.2 step 6 |
| Multi-workspace isolation | ✅ | D22, §3.3/4.3 step 2 |

### 11.4 Internal consistency checks

| Check | Result |
|---|---|
| `caller` naming used consistently in contract.md and decisions.md | ✅ (Oracle verified, fixed in D24) |
| `session_index_store` used (not `session_store`) | ✅ (Oracle verified, fixed in §4.3) |
| `output_path`/`trace_dir` sourced from `AgentSendResult` | ✅ (Oracle verified, fixed in §3.3 step 9) |
| `WorkspaceResolver.resolve()` returns tuple, not bare resources | ✅ (Oracle noted, §3.3/4.3 step 2 clarified) |
| D10 "No separate caller model" does not contradict D24 | ✅ (D10 clarified: means no auth/lookup endpoint) |
| `BotRecordScope` field names match actual code | ✅ (`workspace_id`, `pool`, `session_id` — scope.py:183) |
| `PoolWorkspaceResources` attribute names match actual code | ✅ (`session_index_store`, `pool_data`, `pools`, `workspace_transcript_store` — handle.py:112-122) |
| `CommunicationTarget.kind` is `AgentCommKind` | ✅ (tools.py:58, service.py:172) |
| `AgentSendResult.is_peer_send` exists | ✅ (result.py:22) |
| `AgentSendResult.output_path`/`trace_dir` exist | ✅ (result.py:22-23) |
| `_materialize_events` is module-level function | ✅ (transcript_store.py:192) |
| `load_all_messages()` includes soft-deleted | ✅ (split_stores.py:65-90) |
| Legacy `modexctl.main` private functions imported by `modexbot` | ✅ (§10.1 table B) |
| Root `pyproject.toml` line 100 has `modexctl` script | ✅ (§10.1 table A) |
| `postinstall.py` line 111 references old `modexctl.main` | ✅ (§10.1 table C) |

### 11.5 Completeness assessment

| Area | Status | Notes |
|---|---|---|
| HTTP request models | ✅ Complete | `SendRequest`, `HistoryRequest` with Pydantic definitions |
| HTTP response models | ✅ Complete | `SendResult`, `HistoryResult`, `HistoryMessage`, `DispatchOutcome` |
| Error model | ✅ Complete | `ControlError` with 11 error codes, HTTP status mapping |
| Internal send flow | ✅ Complete | 10-step pipeline from parse to response |
| Internal history flow | ✅ Complete | 8-step pipeline covering native and external sources |
| Package structure | ✅ Complete | `bot/control/` with 6 modules + `workspace/request_resolver.py` |
| CLI adaptation | ✅ Complete | Env→request mapping, send flow, history flow |
| Deployment: local dev | ✅ Complete | §9.6 with full chain trace |
| Deployment: packaged Windows | ✅ Complete | §9.7-9.8 with postinstall changes |
| Legacy deprecation | ✅ Complete | §10 with coupling map, 5-step strategy, safety table |
| Decision log (D1-D24) | ✅ Complete | All resolved, Oracle-reviewed |
| Glossary | ✅ Complete | Separate file with all terms |
| CONTEXT.md | ✅ Complete | Bot domain language updated |

### 11.6 Remaining implementation decisions (non-blocking)

These are implementation choices that do not require design approval before
coding begins:

1. Whether to add a public `send_structured()` method on
   `AgentCommunicationService` or elevate `_send()` to public.
2. Whether invocation existence check uses `SessionRegistry` (in-memory) only
   or also consults `SessionIndexStore` (persistent) for evicted sessions.
3. Whether `request_resolver.py` returns a structured `WorkspaceResolution`
   dataclass or continues returning `Path` with separate validation.
4. Whether history's `PoolData` read access needs turn-pinning or is safe
   read-only without it.
5. Exact location of new CLI test suite
   (`examples/bot_project/tests/unit/cli/` vs `tests/unit/cli/modexctl/`).
6. Whether to add a `# DEPRECATED` docstring to `src/modexctl/__init__.py`
   or rely on D2 documentation alone.

None of these change the HTTP contract, CLI surface, deployment chain, or
deprecation strategy. They are local implementation choices for the
implementing engineer.
