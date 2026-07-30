# Child session capture — design note

Status: implemented (2026-07-30)
Parent ADR: ADR-0022 (`docs/adr/0022-external-coding-agent-integration.md`)
Parent spec: `docs/design/external-coding-agent-integration/spec.md`
Module doc: `src/modex_agent/agents/external_coding/AGENTS.md` (§ Child Session Capture)

## Problem

opencode (and future Claude Code) forks internal subagent sessions at
runtime — transient child sessions that execute tool calls and produce
text/reasoning output. Under the original ADR-0022 design, the
`OpenCodeSSEParser` parsed SSE events from the main session only. Events
carrying a different `sessionID` (a child fork) were either dropped or
misrouted to the main-session emitter, making internal subagent work
invisible in the WebUI transcript and absent from the session tree.

The operator saw a main agent turn that appeared to produce no output
while the provider was actually running a subagent internally — the
child's text, tool calls, and tool results were lost.

## Solution

A provider-neutral discovery pipeline that makes provider-internal child
sessions first-class ModexAgent sessions:

1. **`ChildSessionDiscoverySink` ABC** (`child_discovery.py`) — isolates
   the discovery mechanism so the `ExternalCodingAgent` harness and the
   persistence layer (`ExternalSessionMapStore`) stay provider-neutral.
   A new provider family (Claude Code, Codex, Cursor) implements this
   ABC; the harness and persistence layer are unchanged.

2. **Discovery in `_handle_emission`** — when
   `Emission.source_session_id` is set (non-None), the emission
   originates from a provider-discovered child session. The first time a
   child is seen, discovery runs **synchronously** in the same call:
   resolve the deterministic modex session_id, populate the routing
   mapping, and create the child emitter — all before the first child
   emission is routed. The async registration side-effect
   (`SessionRegistry.register` + `ExternalSessionMapStore.commit`) fires
   as a tracked background task gathered in `_run_turn`'s finally block.

3. **Per-child emitters** — each discovered child gets its own
   `ContentEmitter` (via `child_emitter_factory`). Child emissions
   (text, tool calls, tool results) route to the child emitter, not the
   main-session emitter. The main-session emitter sees only main-session
   events. Tool call/result accumulators are per-child, so `call_id`
   matching stays isolated — no cross-talk between concurrent children.

## Key design decisions

### Sync resolve + async side-effects

The two ABC methods are split by side-effect, not by identity:

- `resolve_child_modex_session_id` is **sync** and side-effect-free. It
  deterministically derives the modex session_id via `encode_snowflake`
  so the routing mapping and child emitter can be populated *before* the
  first child emission is handled. No await race window — the first
  child emission is never dropped.
- `on_child_discovered` is **async**. It fires
  `SessionRegistry.register` + `ExternalSessionMapStore.commit` as a
  fire-and-forget background task. The task reference is retained
  (`_pending_child_tasks`) and gathered in `_run_turn`'s finally block
  so registration completes within the turn boundary.

Both methods feed the same `provider_child_sid` + fixed agent name
(`"external-subagent"`) through `SessionIdFactory.create`, so they
observe the same deterministic modex session_id.

### Deterministic session IDs

`encode_snowflake` (in `core/session_id.py`) hashes
`provider_child_sid` through SHA-256 → base58, producing a compact,
filesystem-safe, deterministic prefix. The same `provider_child_sid`
always maps to the same modex session_id across turns, so:

- Cross-turn resume reuses the same `SessionInfo` and `SessionMapEntry`
  without duplication.
- `SessionRegistry.register` merges (updates `updated_at`, metadata)
  rather than creating a new record on the second turn.
- The WebUI session tree shows one child node per child session, not
  one per turn.

### Provider-neutral ABC

`ChildSessionDiscoverySink` imports no provider-specific types. The
harness wires a concrete `ExternalChildSessionDiscoverySink` that calls
`encode_snowflake` and persists the mapping through
`ExternalSessionMapStore`. A new provider family implements the ABC;
the harness, persistence layer, and WebUI are unchanged.

### `parent_modex_session_id` on `SessionMapEntry`

`SessionMapEntry.parent_modex_session_id` is `None` for main-session
mappings (backward compatible with pre-capture callers) and set to the
parent's modex session_id for child mappings.
`ExternalSessionMapStore.resolve_child(parent_modex_sid)` returns only
child entries. `SessionStore.get_children(parent_modex_sid)` returns
child `SessionInfo` records by filtering on
`SessionInfo.parent_session_id`. The WebUI `buildTree()` pure function
groups the flat session list into a parent→children tree by matching
`parent_session_id` to `session_id` — child sessions appear nested
under their parent in the sidebar with no WebUI code change.

## SSE-only limitation

Child session discovery relies on `Emission.source_session_id`, which
is set by `OpenCodeSSEParser` from the SSE event's `sessionID` field
when it differs from the main session. The JSONL stdout parsers
(`OpenCodeEventParser`, `PiEventParser`) do not carry per-event session
IDs, so child sessions are invisible under `opencode run --format json`
and Pi's JSONL output. Only the warm `opencode serve` SSE backend
surfaces child events.

## Testing

Integration tests
(`tests/unit/agents/external_coding/test_external_subagent_capture_integration.py`)
use `ScriptedStreamingAdapter` + `OpenCodeSSEParser` to simulate a
complete opencode SSE event sequence — no real opencode process is
spawned. Coverage:

- **End-to-end**: main text → child text → child tool → main text.
  Verifies main/child emitter routing, child `SessionInfo` registration
  with correct `parent_session_id`, `ExternalSessionMapStore` parent-
  child mapping, `SessionStore.get_children`, and `buildTree()` pure
  function tree construction.
- **Cross-turn resume**: two turns, same `provider_child_sid` →
  `SessionRegistry.register` merge (no duplicate record) + deterministic
  modex session_id.
- **Concurrent children**: two different `provider_child_sid` events
  interleaved → two independent child emitters, no cross-talk.
- **`buildTree()` pure function**: parent + child → parent's `children`
  contains child; sorting, nesting, orphan handling.

## Future

- **REST API backfill (v2)**: opencode's REST API may expose a
  `GET /session/{id}/children` endpoint that returns child session
  metadata. A backfill path could query this after a turn completes to
  capture child sessions whose events were missed (e.g. under the
  JSONL fallback). Deferred until the SSE path is proven in production.
- **Other providers (Pi, Claude Code)**: Pi's JSONL output does not
  carry per-event session IDs. Claude Code's `control_request` channel
  is bidirectional and may surface child sessions differently. Each new
  provider implements `ChildSessionDiscoverySink`; the harness and
  persistence layer are unchanged.
- **`provider_agent_type` enrichment**: the SSE parser currently does
  not extract the child's agent type (e.g. `"coder"`, `"reviewer"`).
  The `on_child_discovered` ABC accepts an optional
  `provider_agent_type` parameter that populates
  `SessionInfo.metadata["provider_agent"]`. Future parser work could
  extract this from the SSE event payload.
