<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-10-01 | module-table accuracy pass (27 packages) -->

# modex_agent

Core multi-agent framework package: abstractions, implementations, the three-layer runtime model (Hook / Interceptor / Control), and bundled capabilities including Experience and Skills.

> [!NOTE]
> "Hook / Interceptor / Control" names three packages, but they are not peers
> at runtime. **Hook and Interceptor are the live extension layers.** The
> `control/` package carries the **live** `/stop` + WebUI-pause mechanism:
> `InMemoryControlChannel` receives `CANCEL_TURN`, `drain_control_channel()`
> feeds `ControlDrainInterceptor` / `LlmCancelInterceptor`, which raise
> `AgentCancelledError` → `AgentResult(stop_reason=CANCELLED)`. A separate busy-input
> INTERRUPT path cancels via `asyncio.Task.cancel()` directly (does not go
> through the channel). See `control/AGENTS.md`.

## Purpose

The `src/modex_agent/` directory is the reusable agent framework. It provides ABCs, runtime engines, memory systems, multi-agent coordination, tool execution, sandboxing, pipeline orchestration, and extension points (hooks, interceptors, plugins). Business wiring lives in `examples/`.

## Module Overview

| Module | Subdirectories | Purpose |
|--------|----------------|---------|
| `core/` | `turn/` | Foundational contracts and values: agents, the turn-event sink seam (`TurnEventSink`/`KindGate`/`TurnBinding`), `MessageHistory`, system-prompt seams, messages, LLMs, tools, media, session identity, canonical `RecordScope`, the inbox/control/interceptor/terminal/guard/storage contracts, and the turn-execution vocabulary in `core/turn/`. Session persistence lives in `persistence/` (see `core/AGENTS.md`). |
| `agents/` | `react/`, `external/`, `summarizer/` | Agent implementations — `ReActAgent`, `ExternalAgent`, and `SessionCompactorAgent` (see `agents/AGENTS.md`). |
| `memory/` | `consolidation/`, `core/`, `injection/`, `layers/`, `prompt_pipeline/`, `prompts/`, `pruned/`, `registry/`, `stores/`, `tools/` | Context management, configurable memory scopes, governance, concrete message histories, session/archive/core memory with pluggable split stores, and the domain-owned assembly face (`assembly.py` governance-chain builders; `summarizer.py` session summarizer; `config.py`/`presets.py` memory config + presets) (see `memory/AGENTS.md`). |
| `persistence/` | `adapters/`, `managers/`, `migrations/`, `session_artifacts/` | Hybrid persistence layer (ADR-0023, ADR-0028~0031). Owns `SessionStore`, `SessionRegistry`, file/SQLite adapters, migrations, and session artifact cleanup. |
| `multi_agent/` | `communication/`, `inbox/`, `pool_config/`, `session_tree/` | Star-topology orchestration — `AgentPool`, `AgentTemplate`, `PoolInstance`, the `ExecutionStrategy` ABC + `StrategyComponentFactory` registration face + `AgentMaterializer` seam, inbox, and `AgentMessageBus` (see `multi_agent/AGENTS.md`) |
| `tools/` | `aci/`, `ast/`, `lint/`, `lsp/`, `mcp/`, `overflow/`, `standard/`, `terminal/`, `web/` | Tool subsystem — concrete `InMemoryToolManager`, filtering, MCP, terminal, overflow, and standard tools (see `tools/AGENTS.md`) |
| `pipeline/` | `input/` | `AgentPipeline` orchestration, `InputAdapter` ABC, snapshot handling, and the generic user-input stage pipeline (`input/` — `InputStage` ABC, envelope, IM/WebUI/ACP stage skeletons with declarative `order`) (see `pipeline/AGENTS.md`; the approval renderer/resumer live in the approval capability bundle, W1-B2) |
| `orchestration/` | — | Framework-level graph orchestration — `GraphOrchestrator` (spec → compiled graph → instance → engine), `GraphControlService`, `GraphRecoveryService`, `GraphSpecLoader` |
| `runtime/` | — | Runtime state governance — `AgentRuntime`/`AgentRuntimeServices`, per-turn `RuntimeContext`, `TurnStateStore` + concrete stores, process identity/registry, snapshot policy (see `runtime/AGENTS.md`). The turn vocabulary (enums/models/dispatch/approval/todo values) lives in `core/turn/`. |
| `commands/` | — | Slash command parsing and dispatch, including the consumer-owned `SkillResolver` command seam (see `commands/AGENTS.md`) |
| `control/` | — | Control transport — `InMemoryControlChannel` (the live `/stop` + pause mechanism), `ControlCommand`, `AgentControlError` exceptions; graph control/recovery live in `orchestration/` (see `control/AGENTS.md`) |
| `hook/` | `builtin/` | Lifecycle hooks — `HookRunner`, `HookPoint`, generic builtin hooks; domain-owned hooks live in `agents/react/hooks/`, `multi_agent/inbox/`, `trace/`, and the capability packages (see `hook/AGENTS.md`) |
| `interceptor/` | `builtin/` | AOP interceptor chain — `InterceptorChain` and builtin interceptors (see `interceptor/AGENTS.md`) |
| `app/` | `models/` | Application-level bootstrap — root `AppConfig` YAML face (incl. the `user_plugins_enabled` opt-out), `config_domain` (deployment-injected app-domain config), the `AppService` lifecycle skeleton, `AppAssemblyRoots` (config/resource/workspace three-root separation), `RunnableAppService` (`runnable.py` — the concrete framework-runnable default: declaration boot → `create_pool` per declared pool + the request-scope single-turn driver; proven end-to-end by `examples/mini_project`, whose main flow also demos the `ConsolePresenter` seam), process `supervisor`, and the model universe (`models/` — registry, choice, provider, assembly) |
| `messaging/` | — | Level-0 message vocabulary — transport models (`InputMessage` et al.), `MessageBroker` + in-memory broker, agent-message vocabulary and routing, message formatting (see `messaging/AGENTS.md`) |
| `plugins/` | `assembly/` (incl. `strategies/`), `defaults/` (incl. `capabilities/`) | Plugin loading (qualified-name discovery over bundled/project/user/entry-point sources, `PluginRegistrationContext` incl. channel-adapter and service-backend registration — broker/control-channel factory faces in `plugins/backends.py`, persistence-backend + external-transport registries in `plugins/persistence_backends.py` + `plugins/external_transports.py`, all on the generic `BackendRegistry` in `core/backend_registry.py`) + unified agent assembly — `create_pool`, the bundled `react`/`external` pool-shape strategies, backend factories, native assembly core; the `CAPABILITY` slot hosts bundled capabilities, including the complete Experience, Skills, Approval, and Sandbox vertical slices (see `plugins/AGENTS.md` and `docs/design/capability-bundles/AUTHOR-GUIDE.md`) |
| `scope/` | — | Scope declarations, validation, compilation, effective toolsets, provenance, the capability compile protocol, and the assembly schema — `ComponentSlot`/`ComponentFactory` (`components.py`), `Capability` (`capability.py`), `RuntimeOwnership`/`StrategyManifest` (`runtime_ownership.py`), `AssemblySpec` (`assembly_spec.py`), `ComponentRegistry` (`component_registry.py`) (see `scope/AGENTS.md`) |
| `providers/` | `http/`, `shared/` | Direct-HTTP event-stream LLM providers, the protocol-engine registry (`protocol_engines.py` — the `interface_format` name resolution face), and protocol engines (ADR-0046; see `providers/AGENTS.md`) |
| `workspace/` | — | Workspace identity, paths, resource lookup, and routing (see `workspace/AGENTS.md`) |
| `trace/` | — | Tracing and observability — `TraceStore`, `TraceHooks`, `TraceType` |
| `utils/` | — | Shared tokenizer, frontmatter, XML, file, process, and time helpers |
| `adapters/` | — | Platform I/O contracts, output adapters, content filters, and the `BufferingSink` delivery-policy bridge onto `OutputAdapter`. The policy is DERIVED from the adapter's `StreamingMode` (`DeliveryPolicy.of_streaming_mode`: NATIVE→STREAMING, NONE→TURN, else SEGMENT) unless the constructor's typed `policy=` override is passed — no channel config key feeds it (see `adapters/AGENTS.md`) |
| `presentation/` | — | Neutral presentation projection over the runtime event seam (ADR-0053, ADR-0054): the closed `PresentationEvent` vocabulary, `TurnEventProjector` ABC + `DefaultTurnEventProjector` (consuming the core `TurnEvent` union directly; eager/lazy turn identity, tool-card pairing, documented ignore-list), the per-turn `SessionEventHub` (a `TurnEventSink` that runs the projector and fans `PresentationEvent`s out to registered `PresentationSink` consumers in order — binding identity, resume continuity without a second `TurnStarted`), the `ConsolePresenter` reference terminal renderer (`console.py` — the seam proof outside the bot, demoed by `examples/mini_project`), and the generic `TranscriptStore` contract with a JSONL implementation + presentation codec and turn-view materialization. Bot WebUI ServerEvents/transcripts are consumer implementations of this contract. |
| `acp/` | — | ACP (Agent Client Protocol) agent-server surface over editor-spawned stdio — SDK-free backend/handle/interaction seam (`AcpInteraction.emit_presentation` for the editor's presentation-event road, `emit` for the scripted backend's core `TurnEvent` road), `ModexAcpAgent` wire mapping (`events_map` maps BOTH altitudes onto the same session updates), once-only permission round-trip; NOT in the `@register` channel registry (ADR-0049, see `acp/AGENTS.md`) |
| `media/` | — | Concrete media storage, MIME classification, and security gates; contracts live in `core/media.py` (see `media/AGENTS.md`) |

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Exact convenience facade over the final core, messaging, memory, adapters, ReAct, and pipeline owners. |

## For AI Agents

### Working In This Directory
- `from __future__ import annotations` in all modules
- The single emitter face: `Agent.run(context, emitter: TurnEventSink)` — `emit(TurnEvent)` + `flush()`, `wants_streaming()` override point, `KindGate` filtering in the base `emit`, `CompositeTurnEventSink` fan-out; sink factories take a `TurnBinding` (typed turn identity)
- Enums/constants over raw strings, Pydantic BaseModels over dicts for config (rules 10-16)
- Every cross-cutting concern needs an ABC or Protocol — prefer ABC per project rules
- Frozen Pydantic BaseModels for config/value objects (rule 12); runtime objects hold state/connections

### Type Safety (from rules/type-safety.md)
1. Enums/constants over raw strings — `MessageRole`, `MessageType`, `FinishReason`, `StopReason`
2. Typed structures over loose dicts — `ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, `OutputMessage`
3. Typed signatures — no bare `Any`, `list`, `dict`, `object` in framework-facing APIs
4. ABCs before implementations (rule 7 — no Protocols) — no concrete dependency where pluggable contract exists
5. Framework vs examples separation — no example-specific config in framework
6. No dynamic access (`getattr`/`hasattr`) except at real extension boundaries

### Testing
- `pytest tests/unit/ -v` before committing
- Absolute imports (`from modex_agent.xxx`) in tests
- Mock `LLMProvider`, `ControlChannel` — never hit real APIs

### Common Patterns
- `ABC` + `@abstractmethod` for contracts (rule 7 — zero Protocols), Pydantic `BaseModel` for structured data (rules 10-16)
- `scopes: frozenset[InterceptorScope]` for declaring interceptor scope
- Per-turn state in `ctx.state` (typed `ReActTurnState`, a `GraphState(BaseModel)`) for ReAct nodes, not instance attributes
- `GraphInterrupt` (from `modex_graph.exceptions`) for approval suspension — never catch and swallow it
- `TurnCustomKey` enum for per-turn custom state keys in `TurnStateBase.custom`

### Module Responsibilities
- `core/` — Foundational contracts and values, including `MessageHistory` and system-prompt seams; no session persistence or concrete memory adapters.
- `agents/` — General agent strategies (ReAct, external harness, summarizers). Capability-specific agents stay in their capability packages. External provider resources converge through `ExternalTransport.close()`.
- `memory/` — Context, memory scope/governance, concrete histories, and three-layer persistent memory. Split store ABCs + `MemoryStoreBundle` are the storage contract.
- `persistence/` — Session persistence plus hybrid file/SQLite adapters (ADR-0023). The backend name (`persistence.backend`, default registry-resolved: `file`/`sqlite`) selects a `PersistenceBackendBundle` (`plugins/persistence_backends.py`) — plugins register additional bundles.
- `multi_agent/` — Star-topology subagent orchestration.
- `tools/` — Concrete tool manager (InMemoryToolManager), MCP, terminal backends.
- `pipeline/` — End-to-end orchestration pipeline.
- `runtime/` — Runtime state/services and store/codec contracts; the turn vocabulary lives in `core/turn/`.
- `hook/` + `interceptor/` — Extension layers for lifecycle observation and AOP.
- `control/` — Control transport: live `/stop` + pause queues `CANCEL_TURN` and actively cancels the registered turn task so long-running tools wake immediately; ToolNode converges worker cleanup and tool-result synthesis. A separate busy-INTERRUPT path uses the same task-cancel wakeup without a channel command.
- `app/` — Application bootstrap: root `AppConfig` YAML face, `config_domain`, the `AppService` lifecycle skeleton, `AppAssemblyRoots`, `supervisor`, and the model universe (`app/models/`). The former `ioc/` layer was dissolved — configs moved to their owning domains (`memory/config.py`, `trace/observability.py`, `providers/llm_config|model_config|safety_config`, `hook/config.py`, `tools/mcp_config.py`), factories to `providers/factory.py`, `memory/assembly.py`, and `plugins/assembly/*_factory.py`.

## Graph Scheduling Convergence

The graph engine (`modex_graph`) uses a unified scheduling path for normal execution, pause recovery, and crash recovery — no separate recovery engine. See `src/modex_graph/AGENTS.md` for the full design (`bootstrap` entry point, version chain, deliver admission, persistence tradeoff).

From `modex_agent`'s perspective:
- **Execution owner:** `GraphOrchestrator` (`orchestration/graph_orchestrator.py`) reserves one execution/control per instance synchronously and eagerly enters the task's `try/finally` before returning it. Fresh membership and scoped I/O are saved before fallible assembly or suspension; the engine waits for RUNNING output. `start_run`, `start_invoke`, `start_resume`, and awaited execution share admission/assembly. Duplicate starts, resume while draining, and eviction of an owned execution are rejected. `get_graph_context(gid)` exposes the live context through finalization.
- **Drain:** `pause` / `stop` persist and emit `PAUSING` / `STOPPING`, signal the same graph instance, and shield the wait for its actual exit. The owner drains node cleanup and output before publishing `PAUSED` / `STOPPED`, retaining admission until final output settles; stop can upgrade a pending pause. Scheduler drain and owner finalization share `GraphRunControl.wait_for_settlement`, preserving cleanup through repeated cancellation. `cleanup()` drains owners before releasing coordinators. `GraphControlService` delegates lifecycle, with no independent status writer or engine registry.
- **Recovery:** manual resume accepts only idle `PAUSED`; automatic recovery selects explicit `CRASHED` only. Process-liveness classification belongs to the business layer, not the absence of a local owner. `_run_existing_instance` delegates to `run_instance(mode=RECOVERY)` without eviction or status prewrites. Shared assembly retains a paused coordinator or reconstructs it from stores, restores node IDs, and creates a new control handle. Cooperative cancellation records node `CANCELED`; recovery re-executes it with consumable inputs rather than restoring an in-flight stack or business-state snapshot.
- **Run membership:** FRESH saves its original graph invocation version in `attrs[GRAPH_RUN_VERSION_KEY]` (`graph_run_version`); recovery increments attempt version but carries that attr forward into context, node records, and I/O. Membership is exact nullable equality, including `None == None` for legacy/unscoped records, never a Snowflake anchor. Missing/mismatched START for a non-null run restarts entry despite older completed history. END-result reuse requires matching-run completed END and latest I/O; recovery placeholders preserve that run's prior output. FRESH re-invoke does not inherit old-run input/output.
- **Limits:** resumed unfinished agent work is at-least-once and may repeat provider/tool effects. Cooperative cancellation cannot preempt synchronous blocking code or roll back external side effects; node/business persistence owns idempotency. Live-provider and hard-kill validation are not implied by the unit/integration contract.

For admission, pause/stop completion, restart resume, or external-deliver changes, read `docs/design/graph-orchestration/external-control.md` for the implemented contract and verification scope.

## Dependencies

### Internal
- All modules depend on `core/` for ABCs and types.
- `agents/` depends on `core/` (agent ABC, graph engine, tool manager).
- `memory/` depends on `core/` for canonical messages, `MessageHistory`, prompt seams, session identity, and `RecordScope`.
- `persistence/` depends on `core/` identity/scope values and `memory/` split-store ABCs; it owns session persistence and backend adapters.
- `multi_agent/` depends on `core/` (agent ABC), `memory/` (isolated memory), `messaging/` (bus), `persistence/` (InboxMQ, routing stores).
- `pipeline/` depends on `core/`, `agents/`, `runtime/`, `commands/`.
- `tools/` depends on `core/` (Tool ABC, ToolManager ABC); owns the concrete InMemoryToolManager (C2).
- `sandbox` is a capability bundle (`plugins/defaults/capabilities/sandbox/`, W1-B3): it integrates with tools/workspace, approval/core/interceptor and runtime contracts, imported lazily at its below-bundle use sites. Substrate opt-in uses agent `interceptors` plus `interceptor_configs.sandbox_guard.sandbox`, not a scope-root sandbox field. DEFAULT creates no sandbox interceptor or probe; independent approval, WebReader safety and native delegation checks remain separate.

### External
- `httpx` — direct-HTTP LLM provider transport (ADR-0046)
- `pydantic` — config models
- `pyyaml` — frontmatter parsing
- `pathvalidate` — filename sanitization
- `pexpect` / `tmux` / `winpty` — terminal backends
- `aiosqlite` — async SQLite driver for the persistence layer (ADR-0023); the CLI uses stdlib `sqlite3`

## Approval & Security Architecture

See [sandbox guidance](plugins/defaults/capabilities/sandbox/AGENTS.md) and the [permission contract](../../docs/design/unified-security/PRD.md) for native-main switch combinations, fixed native delegation and external limits. Sandbox and human approval are independently switchable; guard-only checks still reuse `ApprovalRuntime`. Enabled main approval escalates BOUNDARY even with an empty tools map; native subagents return direct errors without human escalation. HOST command guards are best-effort, external provider tools bypass framework ToolNode, and fallback never grants permission or replays a possibly-submitted command. [Ticket evidence](../../docs/design/sandbox-integration/tickets.md#validation-evidence) records Windows/WSL validation and live-platform gaps.

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->
