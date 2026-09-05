<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 -->

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
| `core/` | — | Foundational contracts and values: agents, emitters, `MessageHistory`, system-prompt seams, messages, LLMs, tools, media, session identity, and canonical `RecordScope`. Session persistence lives in `persistence/` (see `core/AGENTS.md`). |
| `agents/` | `react/`, `external/`, `summarizer/` | Agent implementations — `ReActAgent`, `ExternalAgent`, and `SessionCompactorAgent` (see `agents/AGENTS.md`). |
| `memory/` | `consolidation/`, `core/`, `injection/`, `layers/`, `prompt_pipeline/`, `prompts/`, `pruned/`, `registry/`, `stores/`, `tools/` | Context management, configurable memory scopes, governance, concrete message histories, and session/archive/core memory with pluggable split stores (see `memory/AGENTS.md`). |
| `persistence/` | `adapters/`, `managers/`, `migrations/`, `session_artifacts/` | Hybrid persistence layer (ADR-0023, ADR-0028~0031). Owns `SessionStore`, `SessionRegistry`, file/SQLite adapters, migrations, and session artifact cleanup. |
| `multi_agent/` | `communication/`, `inbox/`, `session_tree/` | Star-topology orchestration — `AgentPool`, `AgentTemplate`, `PoolInstance`, inbox, and `AgentMessageBus` (see `multi_agent/AGENTS.md`) |
| `tools/` | `ast/`, `lsp/`, `mcp/`, `overflow/`, `standard/`, `terminal/`, `web/` | Tool subsystem — concrete `InMemoryToolManager`, filtering, MCP, terminal, overflow, and standard tools (see `tools/AGENTS.md`) |
| `sandbox/` | `adapters/` | Opt-in execution substrate and shared permission judgments (ADR-0007): LOCAL/OCI selection, per-session native main/subagent HOST fallback, canonical targets and independent human approval. DEFAULT is dormant; HOST/external coverage and validation limits: see `sandbox/AGENTS.md`. |
| `pipeline/` | — | `AgentPipeline` orchestration, `InputAdapter` ABC, approval renderer, snapshot handling (see `pipeline/AGENTS.md`) |
| `runtime/` | — | `AgentRuntime`, runtime state/codecs, `TurnStateStore`, and per-session Todo models/store contracts in `todo.py` (see `runtime/AGENTS.md`). |
| `commands/` | — | Slash command parsing and dispatch, including the consumer-owned `SkillResolver` command seam (see `commands/AGENTS.md`) |
| `control/` | — | Control transport — `InMemoryControlChannel` (the live `/stop` + pause mechanism), `ControlCommand`, `AgentControlError` exceptions (see `control/AGENTS.md`) |
| `hook/` | `builtin/` | Lifecycle hooks — `HookRunner`, `HookPoint`, builtin hooks (see `hook/AGENTS.md`) |
| `interceptor/` | `builtin/` | AOP interceptor chain — `InterceptorChain` and builtin interceptors (see `interceptor/AGENTS.md`) |
| `ioc/` | `configs/`, `factories/` | `AppConfig` and typed construction helpers (see `ioc/AGENTS.md`) |
| `approval/` | — | Tiered tool approval, typed classification facts, per-tool path/command rules and response parsing; guard verdicts reuse the existing transaction/GraphInterrupt channel (see `approval/AGENTS.md`) |
| `messaging/` | — | `MessageBroker`, `BrokerBridgeService` (see `messaging/AGENTS.md`) |
| `plugins/` | `assembly/`, `defaults/capabilities/` | Plugin-unified agent assembly; the `CAPABILITY` slot hosts bundled capabilities, including the complete Experience and Skills vertical slices (see `plugins/AGENTS.md` and `docs/design/capability-bundles/AUTHOR-GUIDE.md`) |
| `scope/` | — | Scope declarations, validation, compilation, effective toolsets, provenance, and the capability compile protocol (see `scope/AGENTS.md`) |
| `providers/` | `http/` | Direct-HTTP event-stream LLM providers and protocol engines (ADR-0046; see `providers/AGENTS.md`) |
| `workspace/` | — | Workspace identity, paths, resource lookup, and routing (see `workspace/AGENTS.md`) |
| `input_pipeline/` | — | Extensible user-input stage pipeline — `UserInputEnvelope`, `InputStage`, `Continue`/`Terminate`, `UserInputPipeline` (see `input_pipeline/AGENTS.md`) |
| `trace/` | — | Tracing and observability — `TraceStore`, `TraceHooks`, `TraceType` |
| `utils/` | — | Shared tokenizer, frontmatter, XML, file, process, and time helpers |
| `adapters/` | — | Platform I/O contracts, output adapters, content filters, and the emitter bridge (see `adapters/AGENTS.md`) |
| `media/` | — | Concrete media storage, MIME classification, and security gates; contracts live in `core/media.py` (see `media/AGENTS.md`) |

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Exact convenience facade over the final core, messaging, memory, adapters, ReAct, and pipeline owners. |

## For AI Agents

### Working In This Directory
- `from __future__ import annotations` in all modules
- Generic type bindings: `Agent[E]`, `ContentEmitter[E]` via `TypeVar("E", bound=AgentEvent)`
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
- `agents/` — General agent strategies (ReAct, external harness, summarizers). Capability-specific agents stay in their capability packages. External provider resources converge through `StreamingProviderBackend.close()`.
- `memory/` — Context, memory scope/governance, concrete histories, and three-layer persistent memory. Split store ABCs + `MemoryStoreBundle` are the storage contract.
- `persistence/` — Session persistence plus hybrid file/SQLite adapters (ADR-0023). `PersistenceBackend` (`FILE`/`SQLITE`) drives IOC selection.
- `multi_agent/` — Star-topology subagent orchestration.
- `tools/` — Concrete tool manager (InMemoryToolManager), MCP, terminal backends.
- `pipeline/` — End-to-end orchestration pipeline.
- `runtime/` — Runtime state/services plus Todo values and persistence contracts.
- `hook/` + `interceptor/` — Extension layers for lifecycle observation and AOP.
- `control/` — Control transport: live `/stop` + pause queues `CANCEL_TURN` and actively cancels the registered turn task so long-running tools wake immediately; ToolNode converges worker cleanup and tool-result synthesis. A separate busy-INTERRUPT path uses the same task-cancel wakeup without a channel command.
- `ioc/` — Dependency injection configuration and factories.

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
- `sandbox/` integrates with tools/workspace, approval/core/interceptor and runtime contracts. Substrate opt-in uses agent `interceptors` plus `interceptor_configs.sandbox_guard.sandbox`, not a scope-root sandbox field. DEFAULT creates no sandbox interceptor or probe; independent approval, WebReader safety and native delegation checks remain separate.

### External
- `httpx` — direct-HTTP LLM provider transport (ADR-0046)
- `pydantic` — config models
- `pyyaml` — frontmatter parsing
- `pathvalidate` — filename sanitization
- `pexpect` / `tmux` / `winpty` — terminal backends
- `aiosqlite` — async SQLite driver for the persistence layer (ADR-0023); the CLI uses stdlib `sqlite3`

## Approval & Security Architecture

See [sandbox guidance](sandbox/AGENTS.md) and the [permission contract](../../docs/design/unified-security/PRD.md) for native-main switch combinations, fixed native delegation and external limits. Sandbox and human approval are independently switchable; guard-only checks still reuse `ApprovalRuntime`. Enabled main approval escalates BOUNDARY even with an empty tools map; native subagents return direct errors without human escalation. HOST command guards are best-effort, external provider tools bypass framework ToolNode, and fallback never grants permission or replays a possibly-submitted command. [Ticket evidence](../../docs/design/sandbox-integration/tickets.md#validation-evidence) records Windows/WSL validation and live-platform gaps.

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->
