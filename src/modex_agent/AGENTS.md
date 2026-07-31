<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# modex_agent

Core multi-agent framework package (334+ Python files across 25 modules). All abstractions, implementations, and the three-layer runtime model (Hook / Interceptor / Control) plus Approval, Experience, and Media.

> [!NOTE]
> "Hook / Interceptor / Control" names three packages, but they are not peers
> at runtime. **Hook and Interceptor are the live extension layers.** The
> `control/` package carries the **live** `/stop` + WebUI-pause mechanism:
> `InMemoryControlChannel` receives `CANCEL_TURN`, `drain_control_channel()`
> feeds `ControlDrainInterceptor` / `LlmCancelInterceptor`, which raise
> `AgentCancelled` → `AgentResult(stop_reason=CANCELLED)`. A separate busy-input
> INTERRUPT path cancels via `asyncio.Task.cancel()` directly (does not go
> through the channel). See `control/AGENTS.md`.

## Purpose

The `src/modex_agent/` directory is the reusable agent framework. It provides ABCs, runtime engines, memory systems, multi-agent coordination, tool execution, sandboxing, pipeline orchestration, and extension points (hooks, interceptors, plugins). Business wiring lives in `examples/`.

## Module Overview

| Module | Files | Subdirectories | Purpose |
|--------|-------|----------------|---------|
| `core/` | 25 py | `skills/`, `experience/` | ABCs — `Agent[E]`, `ContentEmitter[E]`, `Tool`, `ContextManager`, `SessionArtifactCleaner`/`SessionDatabaseCleaner`, types (see `core/AGENTS.md`). The graph engine was extracted to `modex_graph` (ADR-0033). |
| `agents/` | 2 py | `react/`, `external/`, `experience/`, `summarizer/` | Agent implementations — `ReActAgent` (built on `modex_graph`), `ExternalAgent` (Pi/OpenCode CLI harness), `ExperienceReviewAgent`. The deprecated `SummarizerAgent` was removed (ADR-0033 D10). (see `agents/AGENTS.md`) |
| `memory/` | 17 py | `consolidation/`, `core/`, `injection/`, `layers/`, `pipeline/`, `prompts/`, `pruned/`, `registry/`, `stores/`, `tools/` | Three-layer memory — session/archive/core, compaction, consolidation, governance, injection. Split store ABCs (`MessageStore`/`KVStore`/`CursorStore`/`ArchiveStore`) + `MemoryStoreBundle` (see `memory/AGENTS.md`) |
| `persistence/` | 26 py | `adapters/`, `managers/`, `migrations/` | Hybrid persistence layer (ADR-0023, ADR-0028~0031). `ConnectionManager` + `MigrationRunner` (per-workspace SQLite), `PersistenceBackend`/`PersistenceConfig`, `ColumnProjection` (ADR-0030), `SqliteSessionDatabaseCleaner`, SQLite adapters for the split store + runtime-state ABCs. All timestamps are INTEGER ms (ADR-0029) |
| `multi_agent/` | 20 py | `inbox/` | Star-topology orchestration — `AgentPool`, inbox (`InboxMQ`), `AgentMessageBus` (see `multi_agent/AGENTS.md`) |
| `tools/` | 8 py | `ast/`, `lsp/`, `mcp/`, `overflow/`, `standard/`, `terminal/`, `web/` | Tool subsystem — registry, executor, MCP, terminal (pexpect/tmux/winpty), overflow, standard tools (see `tools/AGENTS.md`) |
| `sandbox/` | 17 py | `adapters/` | Sandboxed execution — Subprocess, Docker, E2B, Landlock, guards, environment builder (see `sandbox/AGENTS.md`) |
| `pipeline/` | 7 py | — | `AgentPipeline` orchestration, I/O adapters, approval renderer, snapshot handling (see `pipeline/AGENTS.md`) |
| `runtime/` | 9 py | — | `AgentRuntime`, `AgentRuntimeServices`, `TurnStateStore`, codec, snapshot policy (see `runtime/AGENTS.md`) |
| `commands/` | 7 py | — | Slash command processor — parse, two-stage dispatch, approval/continue/transform actions (see `commands/AGENTS.md`) |
| `control/` | 6 py | — | Control transport — `InMemoryControlChannel` (the live `/stop` + pause mechanism), `ControlCommand`, `AgentControlError` exceptions (see `control/AGENTS.md`) |
| `hook/` | 4 py | `builtin/` | Lifecycle hooks — `HookRunner`, `HookPoint`, 7 builtin hooks (see `hook/AGENTS.md`) |
| `interceptor/` | 4 py | `builtin/` | AOP interceptor chain — `InterceptorChain`, 3 builtin interceptors (see `interceptor/AGENTS.md`) |
| `ioc/` | 2 py | `configs/`, `factories/` | `AppConfig` (Pydantic), 13 typed configs, 8 factory modules (see `ioc/AGENTS.md`) |
| `approval/` | 6 py | — | Tiered tool approval — tiers, decisions, response parsing (see `approval/AGENTS.md`) |
| `messaging/` | 4 py | — | `MessageBroker`, `BrokerBridgeService` (see `messaging/AGENTS.md`) |
| `plugins/` | 5 py | — | Plugin system — `PluginManager`, `PluginContext`, `MemoryProvider` (see `plugins/AGENTS.md`) |
| `providers/` | 3 py | `shared/` | LLM providers — LiteLLM, OpenAI implementations (see `providers/AGENTS.md`) |
| `workspace/` | 13 py | — | `WorkspaceContext` ABC, `DefaultWorkspaceContext` — cd/exit/restore workspace switching with callback notification and persistence (see `workspace/AGENTS.md`) |
| `input_pipeline/` | 5 py | — | Extensible user-input stage pipeline — `UserInputEnvelope`, `InputStage` ABC, `Continue`/`Terminate`, `UserInputPipeline` (see `input_pipeline/AGENTS.md`) |
| `trace/` | 4 py | — | Tracing and observability — `TraceStore`, `TraceHooks`, `TraceType` |
| `utils/` | 11 py | — | tokenizer, context_builder, deduplicator, sanitizer, helpers, `time` (`now_ms`/`now_s` — ADR-0029 single source of truth) |
| `adapters/` | 2 py | — | `PlatformAdapter` ABC, `AdapterRegistry`, `StreamingMode` |
| `media/` | 6 py | — | Attachment/media handling (ADR-0013) — `MediaStore` ABC, MIME classification, security gate, storage routing (`LocalFileMediaStore`) |
| `registry/` | 1 py | — | Shared registry utilities |

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Public API — exports `ReActAgent`, `ReActEvent`, `Agent`, `AgentContext`, `ContentEmitter`, `LLMProvider`, `Tool`, `ToolManager`, `AgentPipeline`, etc. |

## For AI Agents

### Working In This Directory
- `from __future__ import annotations` in all modules
- Generic type bindings: `Agent[E]`, `ContentEmitter[E]` via `TypeVar("E", bound=AgentEvent)`
- Enums/constants over raw strings, Pydantic BaseModels over dicts for config (rules 10-16)
- Every cross-cutting concern needs an ABC or Protocol — prefer ABC per project rules
- Frozen Pydantic BaseModels for config/value objects (rule 12); runtime objects hold state/connections

### Type Safety (from rules/type-safety.md)
1. Enums/constants over raw strings — `MessageRole`, `MessageType`, `FinishReason`, `DefaultValues`
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
- `core/` — ABCs and foundational types. All other modules depend on it.
- `agents/` — Agent strategies (ReAct, external coding CLI harness, summarizer, experience review). External provider resources converge through `StreamingProviderBackend.close()`; adapter-specific lifetime remains local to each backend.
- `memory/` — Three-layer persistent memory with scope isolation. Split store ABCs + `MemoryStoreBundle` are the storage contract.
- `persistence/` — Hybrid persistence (ADR-0023). SQLite `ConnectionManager`/`MigrationRunner` + adapters for the split store and runtime-state ABCs. `PersistenceBackend` (`FILE`/`SQLITE`) drives IOC selection.
- `multi_agent/` — Star-topology subagent orchestration.
- `tools/` — Tool registry, executor, MCP, terminal backends.
- `pipeline/` — End-to-end orchestration pipeline.
- `runtime/` — Runtime state and services assembly.
- `hook/` + `interceptor/` — Extension layers for lifecycle observation and AOP.
- `control/` — Control transport: the live `/stop` + pause channel (`CANCEL_TURN` → drain → interceptors → `AgentCancelled`); plus `AgentControlError` exceptions. A separate busy-INTERRUPT path uses `asyncio.Task.cancel()` directly.
- `ioc/` — Dependency injection configuration and factories.

## Dependencies

### Internal
- All modules depend on `core/` for ABCs and types.
- `agents/` depends on `core/` (agent ABC, graph engine, tool manager).
- `memory/` depends on `core/` (types, context, events, scope).
- `persistence/` depends on `core/` (scope, cleanup) and `memory/` (split store ABCs); implements the SQLite adapters.
- `multi_agent/` depends on `core/` (agent ABC), `memory/` (isolated memory), `messaging/` (bus), `persistence/` (InboxMQ, routing stores).
- `pipeline/` depends on `core/`, `agents/`, `runtime/`, `commands/`.
- `tools/` depends on `core/` (Tool ABC, ToolManager).
- `sandbox/` depends on `core/` (types) only; NOT wired into `tools/` — opt-in capability per ADR-0007.

### External
- `openai` — LLM provider
- `litellm` — LLM provider abstraction
- `pydantic` — config models
- `pyyaml` — frontmatter parsing
- `pathvalidate` — filename sanitization
- `pexpect` / `tmux` / `winpty` — terminal backends
- `aiosqlite` — async SQLite driver for the persistence layer (ADR-0023); the CLI uses stdlib `sqlite3`

## Approval & Security Architecture

See root `AGENTS.md` for a detailed breakdown of the approval architecture, coverage gaps (command content, subagents, pool mode, SSRF, workspace boundary, environment isolation), and what NOT to do.

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->

