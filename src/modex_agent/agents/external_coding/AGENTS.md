<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-07-15 -->

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
