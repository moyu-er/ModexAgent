<!-- Generated: 2026-05-16 | Updated: 2026-05-16 -->

# framework

Core multi-agent framework package. All abstractions, implementations, and the four-layer runtime model (Hook / Interceptor / Control / Approval).

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `core/` | ABCs, AgentContext, events, emitter, provider, tool manager, graph engine, skills (see `core/AGENTS.md`) |
| `agents/` | ReActAgent (graph-based), SummarizerAgent (see `agents/AGENTS.md`) |
| `approval/` | Tiered tool approval — tiers, decisions, response parsing (see `approval/AGENTS.md`) |
| `pipeline/` | AgentPipeline orchestration, I/O adapters, approval renderer (see `pipeline/AGENTS.md`) |
| `session/` | AgentSession — request/response mode |
| `control/` | Runtime control plane — ControlChannel, EventBus, TurnStateStore, ui/ (see `control/AGENTS.md`) |
| `hook/` | Lifecycle hooks — HookRunner, HookPoint, 10 builtin hooks (see `hook/AGENTS.md`) |
| `interceptor/` | AOP interceptor chain — InterceptorChain, 8 builtin interceptors (see `interceptor/AGENTS.md`) |
| `memory/` | Three-layer memory — session/archive/knowledge, compaction, consolidation, injection (see `memory/AGENTS.md`) |
| `multi_agent/` | Star-topology orchestration — pool, subagent_manager, inbox, factory (see `multi_agent/AGENTS.md`) |
| `tools/` | Tool subsystem — registry, executor, MCP, standard tools (see `tools/AGENTS.md`) |
| `plugins/` | Plugin system — PluginManager, PluginContext, MemoryProvider (see `plugins/AGENTS.md`) |
| `messaging/` | MessageBroker, BrokerBridgeService (see `messaging/AGENTS.md`) |
| `providers/` | LLM providers — LiteLLM, OpenAI implementations |
| `ioc/` | AppConfig, typed configs, factory layer (agent, LLM, memory, tools, governance) |
| `runtime/` | AgentRuntime, TurnStateStore, RuntimeCommandStore, codec, snapshot policy (see `runtime/AGENTS.md`) |
| `sandbox/` | Sandboxed execution — Subprocess, Docker, E2B, Landlock (see `sandbox/AGENTS.md`) |
| `security/` | SecurityPolicy, validators, handlers (see `security/AGENTS.md`) |
| `adapters/` | PlatformAdapter ABC, AdapterRegistry, StreamingMode |
| `registry/` | Shared registry utilities |
| `utils/` | tokenizer, context_builder, deduplicator, sanitizer, helpers (see `utils/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- `from __future__ import annotations` in all modules
- Generic type bindings: `Agent[E]`, `ContentEmitter[E]` via `TypeVar("E", bound=AgentEvent)`
- Enums/constants over raw strings, dataclasses over dicts for config
- Every cross-cutting concern needs an ABC

### Testing
- `pytest tests/unit/ -v` before committing
- Absolute imports (`from framework.xxx`) in tests

### Common Patterns
- `Protocol` for contracts, `@dataclass` for data, `ABC` + `@abstractmethod` for abstract classes
- `scopes: frozenset[InterceptorScope]` for declaring interceptor scope
- Per-turn state in `runtime.state` (typed), not instance attributes
- Control commands: `ControlChannel` inbound; events: `ControlEventBus` outbound
