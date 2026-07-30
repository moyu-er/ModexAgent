<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-30 -->

# external_coding

Framework harness for running external coding-agent CLIs as NORMAL main agents
of dedicated pools. The module translates ModexAgent turn/session/peer identity
into provider execution, projects provider events onto canonical `TurnEvent`
models, and owns provider resources through one lifecycle interface.

## Architecture

- `ExternalCodingAgent` owns turn orchestration and retryable agent stop.
- `StreamingProviderBackend` is the execution and cleanup seam. Upper layers
  call `execute_streaming()` and `close()` without branching on provider kind.
- `ExternalSessionMapStore` owns ModexAgent-to-provider session mappings. FILE
  and SQLite adapters share the same resolve/commit/invalidate contract.
- `ProviderEventParser` isolates provider wire formats. The harness is the only
  adapter from provider `Emission` records to canonical `TurnEvent` models.
- `ExternalEnvBuilder`, runtime AGENTS.md injection, and `ExternalPaths`
  centralize provider-visible identity and filesystem layout.
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
| `env_builder.py` | Per-turn `MODEX_*` environment construction |
| `runtime_config.py` | Idempotent provider-visible AGENTS.md marker block |
| `system_prompt.py` | Dynamic peer list and `modexctl send` instructions |
| `paths.py` | Workdir-contained `.modex/external/` paths and `ProviderKind` |
| `os_layer.py` | Cross-platform process-tree lifecycle primitives |
| `scripted_backend.py` | Provider-free deterministic test adapter |
| `turn_runner.py` | Pipeline turn-runner adapter for external agents |
| `providers/opencode_server_backend.py` | Warm `opencode serve` SSE backend |
| `providers/opencode_backend.py` | Per-turn `opencode run` fallback backend |
| `providers/pi_backend.py` | Per-turn Pi backend |
| `providers/*_parser.py` | Pi/OpenCode JSONL and SSE event parsing |

## Lifecycle Ownership

There are three distinct lifetimes:

1. A turn borrows a backend and must not close persistent resources on normal
   completion.
2. A per-turn subprocess is owned from spawn/register through final `wait()`.
   Normal completion reaps it; cancellation/error/close terminates its complete
   process tree.
3. A persistent OpenCode server is owned by `OpenCodeServerBackend` across
   turns. Failed readiness rolls back startup. Successful backend close is the
   only normal path that ends the warm server.

Lifecycle invariants:

- Spawn/register and close are serialized inside real adapters.
- Successful close is terminal; later execution is rejected.
- Never discard process ownership before the process exits and is reaped.
- Multi-resource close is all-settled before propagating the first failure.
- Cleanup failure must propagate. `ExternalCodingAgent` and `AgentPool` retain
  failed owners so a later shutdown can retry.
- Root OpenCode session `idle` ends a turn; it does not prove child/background
  sessions are quiescent and must not close the warm server.

## Provider Behavior

- OpenCode business wiring prefers `OpenCodeServerBackend`. An
  `SSEUnavailableError` activates a sticky `OpenCodeBackend` fallback for later
  turns. Composite close always attempts both owned adapters.
- Pi and `OpenCodeBackend` are per-turn subprocess adapters.
- Session continuity is provider-specific but storage-neutral: Pi resumes a
  workdir-contained JSONL path; OpenCode resumes a provider-minted id.
- Provider-native session data is the context source of truth. ModexAgent's
  transcript is a UI projection and is not fed back as provider memory.

## Child Session Capture

External coding providers (opencode, future Claude Code) fork internal
subagent sessions at runtime — invisible to the harness under the original
ADR-0022 design. The child-session capture pipeline makes those forks
first-class ModexAgent sessions: discovered, registered, routed, and
rendered in the WebUI session tree alongside every other session.

### Discovery sink

`ChildSessionDiscoverySink` (ABC, `child_discovery.py`) isolates the
discovery mechanism so the harness and persistence layer stay
provider-neutral. Two methods split by side-effect:

- `resolve_child_modex_session_id(provider_child_sid) -> str` — **sync**,
  side-effect-free. Deterministically derives the modex session_id via
  `encode_snowflake` so the routing mapping and child emitter can be
  populated *before* the first child emission is handled. No await race
  window.
- `on_child_discovered(provider_child_sid, parent_modex_sid,
  provider_agent_type?) -> str` — **async**. Fires
  `SessionRegistry.register` + `ExternalSessionMapStore.commit` as a
  fire-and-forget background task gathered in `_run_turn`'s finally block.

`ExternalChildSessionDiscoverySink` is the concrete implementation wired
to `SessionIdFactory` + `SessionRegistry` + `ExternalSessionMapStore`.
Both ABC methods feed the same `provider_child_sid` + fixed agent name
(`"external-subagent"`) through `SessionIdFactory.create`, so they
observe the same deterministic modex session_id.

### Routing in `_handle_emission`

When `Emission.source_session_id` is set (non-None), the emission
originates from a provider-discovered child session. The first time a
child is seen, discovery runs synchronously in the same call:

1. `resolve_child_modex_session_id` → deterministic modex session_id
2. Populate `_child_sid_to_modex_sid[provider_child_sid] = modex_sid`
3. Create child emitter via `child_emitter_factory(modex_sid)`
4. Schedule `on_child_discovered` as a tracked background task

Steps 1–3 are sync so the first child emission is routed to the newly
created child emitter in the same call — no drop. Step 4 is async
fire-and-forget; the task reference is retained and gathered in
`_run_turn`'s finally block so registration completes within the turn
boundary.

Per-turn child routing state (`_child_sid_to_modex_sid`,
`_child_emitters`, `_child_accumulators`, `_pending_child_tasks`) is
reinitialized at the top of each `_run_turn` and cleared in the finally
block. Never cross-turn.

### Deterministic session IDs

`encode_snowflake` (in `core/session_id.py`) hashes
`provider_child_sid` through SHA-256 → base58, producing a compact,
filesystem-safe, deterministic prefix. The same `provider_child_sid`
always maps to the same modex session_id across turns, so cross-turn
resume reuses the same `SessionInfo` and `SessionMapEntry` without
duplication. `SessionRegistry.register` merges (updates `updated_at`,
metadata) rather than creating a new record on the second turn.

### SSE-only limitation

Child session discovery relies on `Emission.source_session_id`, which
is set by `OpenCodeSSEParser` from the SSE event's `sessionID` field
when it differs from the main session. The JSONL stdout parsers
(`OpenCodeEventParser`, `PiEventParser`) do not carry per-event session
IDs, so child sessions are invisible under `opencode run --format json`
and Pi's JSONL output. Only the warm `opencode serve` SSE backend
surfaces child events.

### Parent-Child Session Relationships

Parent-child relationships for external subagent sessions live **only** in
`SessionInfo.parent_session_id`, persisted via `SessionStore` (the `sessions`
table / JSON files). `SessionStore.get_children(parent_modex_sid)` returns
child `SessionInfo` records by filtering on `parent_session_id`.

`ExternalSessionMapStore` keeps its original flat modex↔provider mapping
(`modex_session_id` ↔ `provider_session_id`) and does **not** store
parent-child linkage — that is the `SessionStore`'s responsibility, identical
to how native subagent sessions work.

The WebUI's `buildTree()` pure function (`sessionTree.ts`) groups the
flat session list into a parent→children tree by matching
`parent_session_id` to `session_id`, so child sessions appear nested
under their parent in the sidebar with no WebUI code change.

## Testing

- Unit tests never require real Pi/OpenCode APIs. Use scripted adapters or
  mocked process/network boundaries.
- Lifecycle tests cover readiness rollback, cancellation, final reap,
  spawn/close races, all-settled cleanup, close retry, concurrent agent/pool
  shutdown, failed-owner retention, and fallback first-error preservation.
- `test_os_layer.py` includes real platform process-tree tests; Windows must
  verify `taskkill /T` removes a spawned grandchild.

## Do Not

- Do not add provider-specific shutdown branches to `ExternalCodingAgent`,
  `AgentPool`, workspace teardown, or service teardown.
- Do not close the OpenCode SSE server at ordinary turn completion.
- Do not swallow `CancelledError` or cleanup failures at an ownership boundary.
- Do not persist provider session mappings outside `ExternalSessionMapStore`.
- Do not construct `MODEX_*` environment keys outside `ExternalEnvBuilder`.
- Do not import `external_coding` provider types into WebUI code; project
  through canonical `TurnEvent` models.

See ADR-0022 and `docs/design/external-coding-agent-integration/`.
