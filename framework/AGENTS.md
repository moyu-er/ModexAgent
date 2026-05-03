<!-- Generated: 2026-04-30 -->

# framework

## Purpose
Core multi-agent framework package. Contains all abstractions, implementations, and the three-layer control system (Hook / Interceptor / Control).

## Key Files
| File | Description |
|------|-------------|

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `core/` | Abstract base classes, AgentContext, events, emitter, provider, tool manager (see `core/AGENTS.md`) |
| `agents/` | Agent reasoning pattern implementations — ReAct, Summarizer (see `agents/AGENTS.md`) |
| `pipeline/` | AgentPipeline end-to-end flow orchestration, I/O adapters |
| `session/` | AgentSession (request/response mode) |
| `control/` | Runtime control plane — ControlChannel, ControlEventBus, RuntimeStateStore, exceptions (see `control/AGENTS.md`) |
| `hook/` | Lifecycle extension points — HookPoint, HookRunner, builtin hooks (see `hook/AGENTS.md`) |
| `interceptor/` | AOP interceptor system — InterceptorChain, builtin interceptors (see `interceptor/AGENTS.md`) |
| `memory/` | Three-layer memory system — core ABCs, managers, compaction, consolidation, stores, injection (see `memory/AGENTS.md`) |
| `multi_agent/` | Multi-agent orchestration — factory, pool, inbox, subagent_manager, skills |
| `tools/` | Tool subsystem — registry, executor, MCP integration, standard tools |
| `plugins/` | Plugin system — PluginManager, PluginContext, MemoryProvider ABC |
| `messaging/` | MessageBroker, BrokerBridgeService (star-topology communication) |
| `sandbox/` | Sandboxed execution — LocalPython, E2B, Docker, Subprocess adapters |
| `security/` | SecurityPolicy, validators, approval handlers |
| `extensions/` | Optional integrations — LiteLLM provider, ChromaDB/FAISS stores, SQLAlchemy sessions |
| `utils/` | MediaProcessor, tokenizer, helpers |
| `adapters/` | InputAdapter / OutputAdapter base classes |

## For AI Agents

### Working In This Directory
- All components use `from __future__ import annotations` for PEP 563
- Generic type bindings via `TypeVar("E", bound=AgentEvent)` — `Agent[E]`, `ContentEmitter[E]`
- Dataclasses over dicts for config types
- Enums/constants over raw strings for all categories/states
- Every cross-cutting concern needs an ABC

### Testing Requirements
- Run `pytest tests/unit/ -v` before committing framework changes
- Use absolute imports (`from framework.xxx`) in tests
- Tag integration tests with `@pytest.mark.integration`

### Common Patterns
- `Protocol` for contracts, dataclass `@dataclass` for plain data
- `scopes: frozenset[InterceptorScope]` for declaring interceptor scopes
- Hook state stored in `ctx.metadata` (session-scoped), not instance attributes
- Control commands flow through `ControlChannel`; events flow through `ControlEventBus`

## Dependencies

### Internal
- All sub-packages reference `framework.core` for base types

### External
- `litellm` — LLM provider support
- `fastapi` — Gateway adapters
- `chromadb`, `faiss`, `sentence-transformers` — Vector memory
- `sqlalchemy[asyncio]` — SQLAlchemy sessions
## Current Runtime Status

The current ReAct runtime is graph-based and integrates hooks, interceptors,
control, approval, and runtime state through explicit runtime services. See
`docs/current-runtime.md` before changing cross-cutting runtime behavior.
