# CLAUDE.md

## Project Overview

**ModexAgent** — a modular multi-agent framework in Python with generic type-safe architecture. Components (Memory, Tool, Agent, Emitter, Adapter) are independent, pluggable modules.

## Evolving the Framework

Early stage, few consumers. **Do NOT maintain backward compatibility.** Remove dead code aggressively. Rename freely. Prefer clean breaking changes over accumulating cruft.

## Development

```bash
uv pip install -e ".[dev,llm,storage,gateway]"  # install
pytest tests/unit/ -v                             # unit tests
pytest tests/integration/ -v -m integration       # integration tests
ruff check framework/                             # lint
ruff format framework/                            # format
mypy framework/                                   # type check
```

## Testing

- Mirror package structure under `tests/unit/`
- Absolute imports (`from framework.xxx`) in tests
- Tag integration tests with `@pytest.mark.integration`

## Directory Structure

```
framework/
├── core/           # ABCs: Agent[E], Emitter, Tool, ContextManager, graph engine, skills, types
├── agents/
│   ├── react/      # ReActAgent (4-node graph: START→LLM→TOOL→END), state, approval, assembler
│   └── summarizer/ # Single-turn summarization agent
├── hook/           # HookRunner, HookPoint enum, 10 builtin hooks
├── interceptor/    # InterceptorChain AOP, 4 scopes: TURN/ITERATION/LLM_STREAM/TOOL_CALL
├── control/        # ControlChannel, ControlEventBus, TurnStateStore, task_supervision, ui/
├── approval/       # TieredToolApprovalClassifier, ApprovalTransaction, deny policy
├── pipeline/       # AgentPipeline, InputAdapter, OutputAdapter, approval_renderer
├── session/        # AgentSession (request/response mode)
├── tools/          # ToolManager, ToolRegistry, MCP integration, standard tools
├── memory/         # Three-layer: Session→Archive→Knowledge, compaction, consolidation, injection
├── multi_agent/    # AgentPool, SubagentManager, inbox, star-topology coordination
├── plugins/        # PluginManager, PluginContext, MemoryProvider ABC
├── messaging/      # MessageBroker, BrokerBridgeService
├── providers/      # LiteLLM, OpenAI provider implementations
├── ioc/            # AppConfig, typed configs, factory layer
├── runtime/        # AgentRuntime, TurnStateStore, RuntimeCommandStore, codec, snapshot
├── sandbox/        # Subprocess, Docker, E2B, Landlock sandbox adapters
├── security/       # SecurityPolicy, validators, handlers
├── adapters/       # PlatformAdapter ABC, AdapterRegistry
├── registry/       # Shared registry utilities
└── utils/          # tokenizer, context_builder, deduplicator, sanitizer, helpers
```

`examples/bot_project/` — QQ Bot demonstrating all subsystems. Pipeline mode (single agent) or Pool mode (multi-agent star topology).

## Architecture

### Core Data Flow

```
InputAdapter → Pipeline → ContextAssembler → ReActAgent.run()
  → GraphEngine: START → LLMNode → ToolNode → ... → END
  → HookRunner: lifecycle events (BEFORE/AFTER_TURN, ITERATION, TOOL_EXECUTION)
  → InterceptorChain: AOP wrapping (timeout, control drain, tool policy)
  → ControlChannel: runtime commands (cancel, steer, inject)
  → ApprovalRuntime: tool classification → suspend/resume via GraphInterrupt
→ OutputAdapter → MemorySystem.flush()
```

### Four-Layer Runtime Model

| Layer | Role | Key Types |
|-------|------|-----------|
| Hook | Lifecycle observer, content transform | `HookRunner`, `HookPoint` |
| Interceptor | AOP onion-chain around execution boundaries | `InterceptorChain`, 4 scopes |
| Control | Runtime command plane (cancel/inject/steer) | `ControlChannel`, `ControlRuntime` |
| Approval | Tool classification + suspend/resume | `ApprovalRuntime`, `GraphInterrupt` |

Design rules:
- Hooks do NOT control flow. Interceptors wrap explicit boundaries. Control is a first-class side channel. Approval suspends via GraphInterrupt.

### Agent Execution

`ReActAgent` runs a 4-node graph via `GraphEngine`:
- **StartNode**: entry, detects resume from suspended approval
- **LLMNode**: LLM call (streaming/non-streaming), governance chain, hook dispatch
- **ToolNode**: classify tools (NORMAL/DANGEROUS/HARDLINE) → approval if PENDING → batch execute → route to LLM or END
- **EndNode**: assemble `AgentResult`

`AgentRuntime` composes process-scope services (hooks, interceptors, control, approval, governance, safety) with turn-scope state (`ReActTurnState`). Two modes: `clean` (no services) and `full` (all enabled).

### Memory System

Three layers with scope isolation (Session/User/Tenant/Agent/Channel/Chat/PeerPair/Composite/Global):
- **Session**: short-term conversation window
- **Archive**: persistent history
- **Knowledge**: long-term (SOUL/USER/MEMORY.md)

Two-phase compaction: trigger → plan → summary → commit (tool-chain-aware). `DreamEngine` offline consolidation. `MemoryInjectionPolicy` (full/restricted).

### Multi-Agent

Star topology: peers communicate only through main agent. `PeerAutoSendHook` safety net. Per-agent isolated Memory/ToolManager/SkillManager. `SubagentManager` supports spawn/spawn_and_wait/cancel.

### IOC Configuration

`AppConfig.from_yaml()` single source with `${VAR}` interpolation. Per-agent config: LLM, memory, tools, skills, approval, governance.

### Skill System

`FileSkillSource` discovers `SKILL.md` → `ProgressiveBuilder` (compact table + on-demand load) → `DirectorySkillCache`.

## Coding Rules

1. **Python 3.12+**, `from __future__ import annotations` in all framework modules.
2. **Enums over strings**: all categories/states/roles as `Enum`/`StrEnum`. No raw strings.
3. **Typed structures over dicts**: use `ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, `OutputMessage`.
4. **Type annotations required**: avoid bare `Any`, `list`, `dict`, `object` in framework APIs.
5. **Abstract before concrete**: ABCs/Protocols for cross-cutting concerns and extension points.
6. **Framework vs examples**: no example-specific config or business assumptions in `framework/`.
7. **No dynamic access**: avoid `getattr`/`hasattr` unless at a real extension boundary.
8. **Generic bindings**: `Agent[E]`, `ContentEmitter[E]` with `TypeVar("E", bound=AgentEvent)`.
9. **Frozen dataclasses for config**: config = pure data, runtime = state/connections.
10. **Per-turn state in `runtime.state`**: typed turn state, not instance attributes or `ctx.metadata`.

## Document Maintenance

Update this file when: adding modules, changing core interfaces, restructuring directories, adding transports/integrations.
