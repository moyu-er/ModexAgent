<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-31 -->

# external

Framework harness for running external coding-agent CLIs as NORMAL main agents
of dedicated pools. The module translates ModexAgent turn/session/peer identity
into provider execution, projects provider events onto canonical `TurnEvent`
models, and owns provider resources through one lifecycle interface.

## Architecture

- `ExternalAgent` owns turn orchestration and retryable agent stop.
- `StreamingProviderBackend` is the execution and cleanup seam. Upper layers
  call `execute_streaming()` and `close()` without branching on provider kind.
- `OpenCodeServerManager` is a process-wide singleton that owns the shared
  `opencode serve` process across ALL OpenCode backends and ALL pools. Lazy
  spawn on first `acquire()`, health watchdog, PID registry, orphan reaping.
- `OpenCodeServerBackend` is a thin wrapper that borrows from the manager via
  `acquire()`. Its `close()` is a no-op; the shared server lifecycle is the
  manager's job, not the backend's.
- `ExternalSessionMapStore` owns ModexAgent-to-provider session mappings. FILE
  and SQLite adapters share the same resolve/commit/invalidate contract.
- `ProviderEventParser` isolates provider wire formats. The harness is the only
  adapter from provider `Emission` records to canonical `TurnEvent` models.
- `ExternalEnvBuilder`, runtime AGENTS.md injection, and `ExternalPaths`
  centralize provider-visible identity and filesystem layout. `env_builder.py`
  also injects `OPENCODE_PERMISSION` to eliminate runtime permission prompts.
- `os_layer.py` centralizes executable resolution, process-group spawn, and
  complete process-tree termination for Windows and POSIX.

## Key Files

| File | Responsibility |
|------|----------------|
| `agent.py` | Harness turn flow, stale-session fresh retry, event projection, shared retryable `stop()` |
| `contracts.py` | Provider backend/parser ABCs |
| `builder.py` | Explicit collaborator assembly for pool registration |
| `session_store.py` | `ExternalSessionMapStore` ABC and local-file adapter |
| `child_discovery.py` | `ChildSessionDiscoverySink` ABC + `ExternalChildSessionDiscoverySink` concrete sink |
| `env_builder.py` | Per-turn `MODEX_*` environment + `OPENCODE_PERMISSION` injection |
| `runtime_config.py` | Idempotent provider-visible AGENTS.md marker block |
| `system_prompt.py` | Dynamic peer list and `modexctl send` instructions |
| `paths.py` | Workdir-contained `.modex/external/` paths and `ProviderKind` |
| `os_layer.py` | Cross-platform process-tree lifecycle primitives |
| `scripted_backend.py` | Provider-free deterministic test adapter |
| `turn_runner.py` | Pipeline turn-runner adapter for external agents |
| `providers/opencode_server_manager.py` | Singleton managing the shared `opencode serve` process: lazy spawn, liveness check, per-workdir SSE readers, orphan reaping, PID registry, watchdog health monitor, `lifecycle()` async context manager, `_respawn()` extension point |
| `providers/opencode_server_backend.py` | Thin wrapper borrowing from manager via `acquire()`. V1 session ops. `close()` is a no-op. |
| `providers/opencode_v2_client.py` | Typed HTTP client. V1 methods (`create_session_v1`, `prompt_async_v1`, `get_session_status_v1`, `get_messages_v1`, `abort_session_v1`) are live; V2 methods are kept for migration but unused. |
| `providers/opencode_v2_parser.py` | SSE event parser for V1+V2 events. `_main_session_ids` set mutated via `add_main_session` / `remove_main_session`. |
| `providers/opencode_v2_sse_reader.py` | Persistent `/event` SSE reader with per-session demux, child auto-discovery, stall reconnect, replay |
| `providers/pi_backend.py` | Per-turn Pi backend |
| `providers/pi_parser.py` | Pi JSONL stdout parser |

## Lifecycle Ownership

There are three distinct lifetimes:

1. A turn borrows a backend and must not close persistent resources on normal
   completion.
2. A per-turn subprocess is owned from spawn/register through final `wait()`.
   Normal completion reaps it; cancellation/error/close terminates its complete
   process tree.
3. The shared `opencode serve` process is owned by the `OpenCodeServerManager`
   singleton across all backends and all turns. Lifecycle is bound to
   `BotService.start()` via `async with OpenCodeServerManager.lifecycle():`. On
   context exit, `_shutdown()` stops the watchdog, waits up to 5s for active
   sessions to drain, then terminates the process. There is no public
   `close_all()`.

Lifecycle invariants:

- Spawn/register and close are serialized inside real adapters.
- Successful close is terminal; later execution is rejected.
- Never discard process ownership before the process exits and is reaped.
- Multi-resource close is all-settled before propagating the first failure.
- Cleanup failure must propagate. `ExternalAgent` and `AgentPool` retain
  failed owners so a later shutdown can retry.
- Root OpenCode session `idle` ends a turn; it does not prove child/background
  sessions are quiescent and must not close the shared server.

## Shared OpenCode Server (OpenCodeServerManager)

`OpenCodeServerManager` is a process-wide singleton. One `opencode serve`
process serves every `OpenCodeServerBackend` in every pool.

- **Lazy spawn**: first `acquire(workdir)` starts the process if it is not
  running. Subsequent acquires share it.
- **Per-workdir SSE readers**: each workdir gets its own `OpenCodeV2SseReader`
  on `/event`, filtered by the `x-opencode-directory` header.
- **PID registry**: the spawned PID is recorded so external observers can
  detect orphaned processes from a previous run.
- **Orphan reaping**: on startup, stale PIDs from a previous run are reaped
  before a fresh spawn.
- **Health watchdog**: a background task checks process health every 5s. If the
  process died, immediate respawn. After 20 consecutive health failures (with
  busy-session grace), forced respawn. `_respawn()` is the extension point for
  a future poll-phase retry path.
- **`lifecycle()`**: `async with OpenCodeServerManager.lifecycle():` is the
  only supported entry. On context exit, `_shutdown()` stops the watchdog,
  waits up to 5s for active sessions, then terminates the process.
- **Converged respawn**: `acquire()` and `_respawn()` share a single
  `_respawn_locked()` critical section, so spawn and respawn take the same
  path. No provider-specific or path-specific branches.

## API Path Selection (V1 vs V2)

V1 is the primary API for session operations. V2 is used only for the
`/api/health` readiness check.

- **V1 session ops** (live): `POST /session`, `POST /session/:id/prompt`,
  `GET /session/active`, `GET /session/:id/context`, `POST /session/:id/abort`.
- **V2 is unused for session ops** because the V2 `SessionRunner` does not
  inject `promptOps`, which makes the `task` tool unavailable on V2. V2
  endpoints are kept on the client for migration but not called.
- The SSE event stream is `/event` (V1). It carries both V1 and V2 event
  types; `OpenCodeV2EventParser` handles both.

## Permission Elimination

Runtime permission prompts are eliminated at config level, not handled at
runtime:

- `env_builder.py` injects
  `OPENCODE_PERMISSION='{"*":"allow","question":"deny"}'` into the spawn
  environment.
- At the opencode registry level this means every permission is auto-allowed
  and the question tool is disabled. No `permission.asked` event fires, so no
  runtime handler is needed.
- `POST /session/:id/abort` is the hard-stop path for cancellation.

## SSE Event Stream

The SSE reader is **persistent and per-workdir**, owned by
`OpenCodeServerManager`. It is not per-turn and not per-backend.

- `register_session(sid, on_emission)` routes emissions to the correct turn
  callback. Call before `start` so events route from the first connection.
  **Output routes are preserved across turns** — `execute_streaming`'s
  `finally` block does NOT call `unregister_session`. This enables subagent
  recovery output to flow into the same turn's emitter after the main session
  resumes from an `inject`. Stale sid cleanup is handled by LRU on the
  `OpenCodeSessionState` registry.
- `restart(new_url)` stops, resets state, and restarts when the opencode server
  URL changes.
- `/event` filters by the `x-opencode-directory` header, so each workdir's
  reader only sees its own sessions.
- The parser's `_main_session_ids` set (mutated via `add_main_session` /
  `remove_main_session`) distinguishes main sessions from child sessions and
  tags `Emission.source_session_id` accordingly.
- `session.status` events with `status.type === "retry"` map to `BUSY`
  (the session is actively retrying, e.g. rate-limit backoff). This preserves
  the old polling semantics where `retry` was treated as active.

V1+V2 event types parsed by `OpenCodeV2EventParser` (payload in `data`, not V1
`properties`):

- `session.next.text.delta` → `TEXT_DELTA`
- `session.next.reasoning.delta` → `THINKING`
- `session.next.tool.called` → `TOOL_USE`
- `session.next.tool.success` → `TOOL_RESULT` (content text or structured JSON)
- `session.next.tool.failed` → `TOOL_RESULT` (error message)
- `session.error` → `ERROR`

## Turn Completion (Event-Driven)

Turn completion is detected by the `TurnCompletionWaiter` state machine
(`turn_waiter.py`), driven by `session.status`/`session.idle` SSE events
fed through the `OpenCodeSessionState` shared registry (`session_state.py`).
The registry hangs off the SSE reader via `attach_session_state()` and
receives every raw event in `_process_event` (dual-path: parser for
output emissions + registry for state tracking).

**State machine:** ACTIVE → QUIESCING → COMPLETE.

- **ACTIVE**: the session tree has busy nodes or recent events.
- **QUIESCING**: all nodes in the session subtree are idle/error. A quiesce
  window (default 3s) starts. Any tree event during the window cancels it
  and returns to ACTIVE.
- **COMPLETE**: the quiesce window elapses with no events. Two-layer REST
  validation (`GET /session/:id/children`) guards against `session.created`
  loss → fake empty tree — checked before entering QUIESCING (for
  single-node trees) and after the window elapses.

**Key invariant:** a logical turn = the **entire session tree** quiescing,
not just the root session going idle. This ensures subagent recovery output
(injected by opencode's internal `inject` mechanism) flows into the same
turn's emitter before the turn ends.

**Disconnect fallback (`_wait_busy_fallback`):** when the SSE reader is
reconnecting (`is_reconnect_pending()`), a lightweight poll confirms the
session became busy (closing the `prompt_async` → busy race). After
reconnect, `rebuild_subtree` does a full REST tree reconstruction. This
is the ONLY use of polling — the primary path is fully event-driven.

**Lifecycle:** the waiter is per-turn (created in `execute_streaming`,
destroyed in `finally` via `unregister_waiter`). The registry is
per-workdir (persistent across turns, attached to the SSE reader).

## Child Session Capture

External coding providers (opencode, future Claude Code) fork internal
subagent sessions at runtime, invisible to the harness under the original
ADR-0022 design. The child-session capture pipeline makes those forks
first-class ModexAgent sessions: discovered, registered, routed, and rendered
in the WebUI session tree alongside every other session.

### Discovery sink

`ChildSessionDiscoverySink` (ABC, `child_discovery.py`) isolates the
discovery mechanism so the harness and persistence layer stay
provider-neutral. Two methods split by side-effect:

- `resolve_child_modex_session_id(provider_child_sid) -> str`: sync,
  side-effect-free. Deterministically derives the modex session_id via
  `encode_snowflake` so the routing mapping and child emitter can be
  populated *before* the first child emission is handled. No await race
  window.
- `on_child_discovered(provider_child_sid, parent_modex_sid,
  provider_agent_type?) -> str`: async. Fires
  `SessionRegistry.register` + `ExternalSessionMapStore.commit` as a
  fire-and-forget background task gathered in `_run_turn`'s finally block.

`ExternalChildSessionDiscoverySink` is the concrete implementation wired
to `SessionIdFactory` + `SessionRegistry` + `ExternalSessionMapStore`.
Both ABC methods feed the same `provider_child_sid` + fixed agent name
(`"external-subagent"`) through `SessionIdFactory.create`, so they
observe the same deterministic modex session_id.

### SSE-driven discovery

`session.created` events carrying a `parentID` are picked up by the SSE
reader, which auto-discovers the child session. The parser tags
`Emission.source_session_id` from the event's `data.sessionID` when it
differs from the main session. The JSONL stdout parsers
(`OpenCodeEventParser`) do not carry per-event session
IDs, so child sessions are invisible under `opencode run --format json`
and Pi's JSONL output. Only the shared `opencode serve` SSE path surfaces
child events.

### Routing in `_handle_emission`

When `Emission.source_session_id` is set (non-None), the emission
originates from a provider-discovered child session. The first time a
child is seen, discovery runs synchronously in the same call:

1. `resolve_child_modex_session_id` → deterministic modex session_id
2. Populate `_child_sid_to_modex_sid[provider_child_sid] = modex_sid`
3. Create child emitter via `child_emitter_factory(modex_sid)`
4. Schedule `on_child_discovered` as a tracked background task

Steps 1-3 are sync so the first child emission is routed to the newly
created child emitter in the same call, no drop. Step 4 is async
fire-and-forget; the task reference is retained and gathered in
`_run_turn`'s finally block so registration completes within the turn
boundary.

Per-turn child routing state lives in `_TurnEmissionContext` (a dataclass
created per `_run_turn` call, captured by the `on_emission` closure, passed
to `_handle_emission`). This is **not** stored in instance attributes —
the same `ExternalAgent` instance serves all sessions in its pool, so
instance variables would crossover when multiple sessions run concurrent
turns. The closure capture guarantees each turn sees its own `modex_sid`,
`paths`, `spec`, and child maps.

### Deterministic session IDs

`encode_snowflake` (in `core/session_id.py`) hashes
`provider_child_sid` through SHA-256 to base58, producing a compact,
filesystem-safe, deterministic prefix. The same `provider_child_sid`
always maps to the same modex session_id across turns, so cross-turn
resume reuses the same `SessionInfo` and `SessionMapEntry` without
duplication. `SessionRegistry.register` merges (updates `updated_at`,
metadata) rather than creating a new record on the second turn.

### Parent-Child Session Relationships

Parent-child relationships for external subagent sessions live **only** in
`SessionInfo.parent_session_id`, persisted via `SessionStore` (the `sessions`
table / JSON files). `SessionStore.get_children(parent_modex_sid)` returns
child `SessionInfo` records by filtering on `parent_session_id`.

`ExternalSessionMapStore` keeps its original flat modex-to-provider mapping
(`modex_session_id` to `provider_session_id`) and does **not** store
parent-child linkage. That is the `SessionStore`'s responsibility, identical
to how native subagent sessions work.

The WebUI's `buildTree()` pure function (`sessionTree.ts`) groups the
flat session list into a parent-to-children tree by matching
`parent_session_id` to `session_id`, so child sessions appear nested
under their parent in the sidebar with no WebUI code change.

## Environment Variable Isolation (opencode Singleton)

The opencode process is a **singleton** — one `opencode serve` process serves
all opencode pools and all sessions. The process env is set at first spawn
and **frozen forever**: `MODEX_SESSION_ID`, `MODEX_AGENT_NAME`, and all other
`MODEX_*` vars point to the *first* session. When a second session runs
concurrently, `modexctl` (executed by opencode's bash tool) would read the
frozen env and route messages to the wrong session — **crossover**.

### Solution: shell.env plugin + per-session env snapshots

1. **`shell.env` plugin** (`providers/opencode/plugins/modex-shell-env.ts`):
   a ~10-line TypeScript plugin registered via `OPENCODE_CONFIG_CONTENT` env
   var (per-process, not written to disk). The plugin hooks into opencode's
   `shell.env` extension point and injects `OPENCODE_SESSION_ID` (the current
   opencode session ID) into every bash subprocess env. This is **per-tool-call**
   — the main session's bash gets the main session ID; a subagent's bash gets
   the subagent's session ID.

2. **Per-session env snapshot files** (`<workdir>/.modex/external/env-snapshots/<provider_sid>.json`):
   written by `server_backend.execute_streaming` after session creation (main
   session) and by `_handle_emission` on child discovery (subagent session).
   Each file contains the `MODEX_*` vars + `PATH` for that specific opencode
   session.

3. **`modexctl` two-path resolution** (`modexctl/main.py:from_env()`):
   - **Path 1 (opencode singleton)**: `OPENCODE_SESSION_ID` is set → read the
     matching snapshot file → construct `ModexCtlContext` from snapshot.
     **Fail-closed**: if the snapshot is missing or corrupt, return `None`
     (do NOT fall through to the frozen process env — that would crossover).
   - **Path 2 (native agent)**: no `OPENCODE_SESSION_ID` → `MODEX_*` vars are
     per-turn correct (contextvar injection by `NativeEnvInjectionHook`) →
     read directly from env.

### Process isolation

`OPENCODE_CONFIG_CONTENT` is a per-process env var set only on the
ModexAgent-spawned opencode process. Other opencode instances (IDE plugin,
manual `opencode run`, same directory) do not have this env var → the plugin
is not loaded → their bash subprocess env is unchanged. The plugin file lives
in the framework package (`providers/opencode/plugins/`) and is located at
runtime via `Path(__file__)`.

### Concurrency safety

- Each opencode session has a unique ID → unique snapshot file → no file
  overwrite between concurrent sessions.
- `shell.env` plugin injects the *current* session ID per-tool-call (not the
  frozen process env value) → each session's `modexctl` reads its own snapshot.
- Native agents use contextvar (`_modex_env`) which is asyncio-task-scoped →
  no crossover with opencode sessions or other native sessions.

## Per-Turn State Isolation (_TurnEmissionContext)

The same `ExternalAgent` instance serves all sessions in its pool. Per-turn
state (`modex_sid`, `paths`, `spec`, child routing maps, emitters, pending
tasks) MUST NOT live in instance attributes — concurrent turns would
overwrite each other, causing child discovery to register children under the
wrong parent session.

**Solution**: `_TurnEmissionContext` dataclass, created per `_run_turn` call,
captured by the `on_emission` closure, passed to `_handle_emission`. Each
turn's closure sees its own values. The finally block clears `turn_ctx.*`
(not `self.*`). No instance variables hold per-turn state.

This is the same isolation principle as `NativeEnvInjectionHook`'s contextvar
(`_modex_env`): per-turn state must be task-scoped, not instance-scoped.

## SubagentAutoSendHook (external)

`SubagentAutoSendHook` notifies the parent agent when an external subagent
turn ends. For external subagents:

- `<replied>` is **omitted** from the XML notification (`replied=None`).
  The old `_check_replied` read a per-workdir `outbox.jsonl` that was never
  written by production code (modexctl sends via HTTP, not file-based outbox).
  A correct per-session send-tracking mechanism does not exist yet — the
  parent agent judges the outcome solely on `<success>`, `<result>`, and
  `<issue>`.
- `external_outbox_path` parameter has been removed from the hook constructor
  (the path was never read).
- The outbox.jsonl clearing in `_run_turn` has been removed (was a no-op on
  an always-empty file, and a crossover source when sessions shared a workdir).

## Provider Behavior

- OpenCode business wiring uses `OpenCodeServerBackend`, which borrows the
  shared `opencode serve` process from `OpenCodeServerManager`. There is no
  fallback mechanism: the manager plus watchdog guarantee reliability, and the
  manager raises `RuntimeError` if the process cannot be brought up.
- Pi is a per-turn subprocess adapter.
- Session continuity is provider-specific but storage-neutral: Pi resumes a
  workdir-contained JSONL path; OpenCode resumes a provider-minted id.
- Provider-native session data is the context source of truth. ModexAgent's
  transcript is a UI projection and is not fed back as provider memory.

## Convergence Rules Applied

- `set_main_session` removed; converged to `add_main_session` /
  `remove_main_session` on the parser.
- The fallback backend class was deleted; the manager plus watchdog guarantee
  reliability.
- `SSEUnavailableError` deleted; never raised. The manager raises
  `RuntimeError` instead.
- `close_all()` removed; replaced by `lifecycle()` + `_shutdown()`.
- `acquire()` and `_respawn()` converged onto a shared `_respawn_locked()`
  critical section.

## Testing

- Unit tests never require real Pi/OpenCode APIs. Use scripted adapters or
  mocked process/network boundaries.
- Lifecycle tests cover readiness rollback, cancellation, final reap,
  spawn/close races, all-settled cleanup, close retry, concurrent agent/pool
  shutdown, and failed-owner retention.
- `test_os_layer.py` includes real platform process-tree tests; Windows must
  verify `taskkill /T` removes a spawned grandchild.
- `opencode_server_manager.py` has its own test suite covering lazy spawn,
  watchdog respawn, orphan reaping, and lifecycle shutdown ordering.

## Do Not

- Do not add provider-specific shutdown branches to `ExternalAgent`,
  `AgentPool`, workspace teardown, or service teardown.
- Do not call `close()` on `OpenCodeServerBackend` expecting the shared
  server to stop. It is a no-op. Use `OpenCodeServerManager.lifecycle()`.
- Do not swallow `CancelledError` or cleanup failures at an ownership boundary.
- Do not persist provider session mappings outside `ExternalSessionMapStore`.
- Do not construct `MODEX_*` environment keys outside `ExternalEnvBuilder`.
- Do not import `external` provider types into WebUI code; project
  through canonical `TurnEvent` models.

See ADR-0022 and `docs/design/external-agent-integration/`.
