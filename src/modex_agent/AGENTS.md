<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# modex_agent

Core multi-agent framework package (336+ Python files across 24 modules). All abstractions, implementations, and the three-layer runtime model (Hook / Interceptor / Control) plus Approval and Experience.

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
| `core/` | 25 py | `graph/`, `skills/`, `experience/` | ABCs — `Agent[E]`, `ContentEmitter[E]`, `Tool`, `ContextManager`, graph engine, types (see `core/AGENTS.md`) |
| `agents/` | 2 py | `react/`, `experience/`, `summarizer/` | Agent implementations — `ReActAgent` (graph-based 4-node), `SummarizerAgent`, `ExperienceReviewAgent` (see `agents/AGENTS.md`) |
| `memory/` | 17 py | `consolidation/`, `core/`, `injection/`, `layers/`, `pipeline/`, `prompts/`, `pruned/`, `registry/`, `stores/`, `tools/` | Three-layer memory — session/archive/knowledge, compaction, consolidation, governance, injection (see `memory/AGENTS.md`) |
| `multi_agent/` | 20 py | `inbox/` | Star-topology orchestration — `AgentPool`, inbox, `AgentMessageBus` (see `multi_agent/AGENTS.md`) |
| `tools/` | 8 py | `ast/`, `lsp/`, `mcp/`, `overflow/`, `standard/`, `terminal/`, `web/` | Tool subsystem — registry, executor, MCP, terminal (pexpect/tmux/winpty), overflow, standard tools (see `tools/AGENTS.md`) |
| `sandbox/` | 17 py | `adapters/` | Sandboxed execution — Subprocess, Docker, E2B, Landlock, guards, environment builder (see `sandbox/AGENTS.md`) |
| `pipeline/` | 7 py | — | `AgentPipeline` orchestration, I/O adapters, approval renderer, snapshot handling (see `pipeline/AGENTS.md`) |
| `runtime/` | 9 py | — | `AgentRuntime`, `AgentRuntimeServices`, `TurnStateStore`, codec, snapshot policy (see `runtime/AGENTS.md`) |
| `commands/` | 7 py | — | Slash command processor — parse, two-stage dispatch, approval/continue/transform actions (see `commands/AGENTS.md`) |
| `control/` | 6 py | — | Control transport — `InMemoryControlChannel` (the live `/stop` + pause mechanism), `ControlCommand`, `AgentControlError` exceptions (see `control/AGENTS.md`) |
| `hook/` | 4 py | `builtin/` | Lifecycle hooks — `HookRunner`, `HookPoint`, 7 builtin hooks (see `hook/AGENTS.md`) |
| `interceptor/` | 3 py | `builtin/` | AOP interceptor chain — `InterceptorChain`, 2 builtin interceptors (see `interceptor/AGENTS.md`) |
| `ioc/` | 2 py | `configs/`, `factories/` | `AppConfig` (Pydantic), 13 typed configs, 8 factory modules (see `ioc/AGENTS.md`) |
| `approval/` | 6 py | — | Tiered tool approval — tiers, decisions, response parsing (see `approval/AGENTS.md`) |
| `messaging/` | 4 py | — | `MessageBroker`, `BrokerBridgeService` (see `messaging/AGENTS.md`) |
| `plugins/` | 5 py | — | Plugin system — `PluginManager`, `PluginContext`, `MemoryProvider` (see `plugins/AGENTS.md`) |
| `providers/` | 3 py | `shared/` | LLM providers — LiteLLM, OpenAI implementations (see `providers/AGENTS.md`) |
| `workspace/` | 13 py | — | `WorkspaceContext` ABC, `DefaultWorkspaceContext` — cd/exit/restore workspace switching with callback notification and persistence (see `workspace/AGENTS.md`) |
| `input_pipeline/` | 5 py | — | Extensible user-input stage pipeline — `UserInputEnvelope`, `InputStage` ABC, `Continue`/`Terminate`, `UserInputPipeline` (see `input_pipeline/AGENTS.md`) |
| `trace/` | 4 py | — | Tracing and observability — `TraceStore`, `TraceHooks`, `TraceType` |
| `utils/` | 10 py | — | tokenizer, context_builder, deduplicator, sanitizer, helpers |
| `adapters/` | 2 py | — | `PlatformAdapter` ABC, `AdapterRegistry`, `StreamingMode` |
| `registry/` | 1 py | — | Shared registry utilities |

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Public API — exports `ReActAgent`, `ReActEvent`, `Agent`, `AgentContext`, `ContentEmitter`, `LLMProvider`, `Tool`, `ToolManager`, `AgentPipeline`, etc. |

## For AI Agents

### Working In This Directory
- `from __future__ import annotations` in all modules
- Generic type bindings: `Agent[E]`, `ContentEmitter[E]` via `TypeVar("E", bound=AgentEvent)`
- Enums/constants over raw strings, dataclasses over dicts for config
- Every cross-cutting concern needs an ABC or Protocol — prefer ABC per project rules
- Frozen dataclasses for config/value objects; runtime objects hold state/connections

### Type Safety (from rules/type-safety.md)
1. Enums/constants over raw strings — `MessageRole`, `MessageType`, `FinishReason`, `DefaultValues`
2. Typed structures over loose dicts — `ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, `OutputMessage`
3. Typed signatures — no bare `Any`, `list`, `dict`, `object` in framework-facing APIs
4. ABCs/Protocols before implementations — no concrete dependency where pluggable contract exists
5. Framework vs examples separation — no example-specific config in framework
6. No dynamic access (`getattr`/`hasattr`) except at real extension boundaries

### Testing
- `pytest tests/unit/ -v` before committing
- Absolute imports (`from modex_agent.xxx`) in tests
- Mock `LLMProvider`, `ControlChannel` — never hit real APIs

### Common Patterns
- `Protocol` for contracts, `@dataclass` for data, `ABC` + `@abstractmethod` for abstract classes
- `scopes: frozenset[InterceptorScope]` for declaring interceptor scope
- Per-turn state in `runtime.state` (typed `ReActTurnState`), not instance attributes
- `GraphInterrupt` for approval suspension — never catch and swallow it
- `TurnCustomKey` enum for per-turn custom state keys in `TurnStateBase.custom`

### Module Responsibilities
- `core/` — ABCs and foundational types. All other modules depend on it.
- `agents/` — Agent reasoning strategies (ReAct, summarizer, experience review).
- `memory/` — Three-layer persistent memory with scope isolation.
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
- `memory/` depends on `core/` (types, context, events).
- `multi_agent/` depends on `core/` (agent ABC), `memory/` (isolated memory), `messaging/` (bus).
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

## Approval & Security Architecture

See root `AGENTS.md` for a detailed breakdown of the approval architecture, coverage gaps (command content, subagents, pool mode, SSRF, workspace boundary, environment isolation), and what NOT to do.

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->

