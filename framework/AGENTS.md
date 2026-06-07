<!-- Updated: 2026-05-31 | Branch: develop_gyt | Commit: 6647e8a -->

# framework

Core multi-agent framework package (336 Python files). All abstractions, implementations, and the three-layer runtime model (Hook / Interceptor / Control) plus Approval.

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `core/` | ABCs — `Agent[E]`, `ContentEmitter[E]`, `Tool`, `ContextManager`, graph engine (`Graph[R]`/`Node[R]`), skills, types (see `core/AGENTS.md`) |
| `agents/` | `ReActAgent` (graph-based 4-node), `SummarizerAgent` (see `agents/AGENTS.md`) |
| `approval/` | Tiered tool approval — tiers, decisions, response parsing (see `approval/AGENTS.md`) |
| `pipeline/` | `AgentPipeline` orchestration, I/O adapters, approval renderer, slash commands (see `pipeline/AGENTS.md`) |
| `session/` | `AgentSession` — request/response mode |
| `control/` | Runtime control plane — `InMemoryControlChannel`, `CallbackControlEventBus`, `ControlCommand`, `ControlScope`, termination exceptions (see `control/AGENTS.md`) |
| `hook/` | Lifecycle hooks — `HookRunner`, `HookPoint`, 6 builtin hooks (see `hook/AGENTS.md`) |
| `interceptor/` | AOP interceptor chain — `InterceptorChain`, 2 builtin interceptors (see `interceptor/AGENTS.md`) |
| `memory/` | Three-layer memory — session/archive/knowledge, compaction, consolidation, governance, injection (see `memory/AGENTS.md`) |
| `multi_agent/` | Star-topology orchestration — `AgentPool`, inbox, `CommunicationTracker`, `AgentMessageBus` (see `multi_agent/AGENTS.md`) |
| `tools/` | Tool subsystem — registry, executor, MCP, terminal (pexpect/tmux/winpty), overflow, standard tools (see `tools/AGENTS.md`) |
| `plugins/` | Plugin system — `PluginManager`, `PluginContext`, `MemoryProvider` (see `plugins/AGENTS.md`) |
| `messaging/` | `MessageBroker`, `BrokerBridgeService` (see `messaging/AGENTS.md`) |
| `providers/` | LLM providers — LiteLLM, OpenAI implementations (see `providers/AGENTS.md`) |
| `ioc/` | `AppConfig` (Pydantic), 13 typed configs, 8 factory modules (see `ioc/AGENTS.md`) |
| `runtime/` | `AgentRuntime`, `AgentRuntimeServices`, `TurnStateStore`, `RuntimeCommandStore`, codec, snapshot policy (see `runtime/AGENTS.md`) |
| `commands/` | Slash command processor — parse, two-stage dispatch, approval/continue/transform actions (see `commands/AGENTS.md`) |
| `sandbox/` | Sandboxed execution — Subprocess, Docker, E2B, Landlock (see `sandbox/AGENTS.md`) |
| `security/` | `SecurityPolicy`, validators, handlers (see `security/AGENTS.md`) |
| `adapters/` | `PlatformAdapter` ABC, `AdapterRegistry`, `StreamingMode` (see `adapters/AGENTS.md`) |
| `registry/` | Shared registry utilities (see `registry/AGENTS.md`) |
| `utils/` | tokenizer, context_builder, deduplicator, sanitizer, media_utils, helpers (see `utils/AGENTS.md`) |
| `workspace/` | `WorkspaceContext` ABC, `DefaultWorkspaceContext` — cd/exit/restore workspace switching with callback notification and persistence (see `workspace/` directory) |

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
- Absolute imports (`from framework.xxx`) in tests

### Common Patterns
- `Protocol` for contracts, `@dataclass` for data, `ABC` + `@abstractmethod` for abstract classes
- `scopes: frozenset[InterceptorScope]` for declaring interceptor scope
- Per-turn state in `runtime.state` (typed `ReActTurnState`), not instance attributes
- Control commands: `ControlChannel` inbound; events: `ControlEventBus` outbound
- `GraphInterrupt` for approval suspension — never catch and swallow it
- `TurnCustomKey` enum for per-turn custom state keys in `TurnStateBase.custom`
