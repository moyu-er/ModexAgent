# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**ModexAgent** (**Mod**ular + **Nex**us + **Agent**) — a lightweight, modular multi-agent framework in Python with a **generic type-safe architecture**. Every component (Memory, Tool, Agent, Emitter, Adapter) is an independent, pluggable module.

1. **Generic Type Safety**: `Agent[E]`, `ContentEmitter[E]`, `EmitterConfig[E]` bind event enums at compile time.
2. **Clean Separation**: Agent (reasoning), Emitter (output), ToolManager (tools), ContextManager (history) are independent.
3. **I/O Agnostic**: `InputAdapter` / `OutputAdapter` decouple I/O from agent logic.
4. **Streaming vs Non-Streaming**: Determined by the `Emitter` implementation, not the `Agent`.
5. **Plugin Extensibility**: Convention-based plugin system for tools, memory providers, hooks, and skills.

## Document Maintenance

This file is the framework's **single source of truth** and must stay in sync with the code. Update this document when:

- **Adding modules or sub-packages** (e.g., new `tools/mcp/`, `security/`, `core/skills/` directories)
- **Changing core interfaces** (e.g., `Agent[E]` signature, `Tool.execute()` return type)
- **Changing architectural patterns** (e.g., new messaging mechanism, memory layer restructuring)
- **Adding transports or integrations** (e.g., new MCP transport type, new LLM provider)
- **Restructuring directories** (e.g., file splits, merges, renames)

Every commit involving the above changes should include a CLAUDE.md review.

## Evolving the Framework

This framework is in its **early stage** with few consumers. When making significant changes:

- **Do NOT maintain backward compatibility** — clean up old, incorrect code and usage patterns directly.
- **Remove dead code aggressively** — unused abstractions, deprecated wrappers, and stale re-exports should be deleted, not kept behind compat shims.
- **Rename freely** — if a name is confusing or inconsistent, rename it. Update all callers.
- **Prefer breaking changes over accumulating cruft** — a clean breaking change now is cheaper than carrying wrong abstractions forward.

## Development Commands

```bash
# Setup (uv is preferred; pip works too)
uv pip install -e ".[dev,llm,storage,gateway]"
# or: pip install -e ".[dev,llm,storage,gateway]"

# Run all unit tests
pytest tests/unit/ -v

# Run a single test
pytest tests/unit/memory/test_auto_compact.py::test_auto_compact_archives_before_prune -xvs

# Run tests for a specific module
pytest tests/unit/memory/ -v
pytest tests/unit/agents/ -v

# Run integration tests
pytest tests/integration/ -v -m integration

# Run e2e tests
pytest tests/e2e/ -v

# Lint
ruff check framework/
ruff check --fix framework/

# Format
ruff format framework/

# Type check
mypy framework/
```

## Testing Standards

```
tests/
├── unit/               # Pure unit tests; no external deps; must run offline
│   ├── core/           ├── agents/          ├── extensions/
│   ├── pipeline/       ├── session/         ├── tools/
│   ├── memory/         ├── multi_agent/     ├── plugins/
│   └── utils/
├── integration/        # Requires config files, LLM APIs, etc.
└── e2e/
```

**Rules:**
1. Mirror package structure under `tests/unit/`.
2. Use absolute imports (`from framework.xxx`) inside tests.
3. Tag integration tests with `@pytest.mark.integration`.

## Directory Structure

```
framework/
├── core/                  # Abstract base classes: Agent, ContextManager, Emitter, Tool, hooks, skills/, runtime_context
├── agents/react/          # ReActAgent with ReActEvent enum
├── pipeline/              # AgentPipeline, InputAdapter, OutputAdapter
├── session/               # AgentSession (request/response mode)
├── tools/                 # ToolRegistry, executor, MCP integration, standard tools
├── memory/                # Three-layer memory system + redesign docs in agent_docs/
│   ├── core/              # ABCs: MemoryScope, MemoryStorage, ChatMessage, scope metadata
│   ├── managers/          # ShortTerm, History, LongTerm layer managers
│   ├── compaction/        # MemoryCompactionPipeline, MessageCompactionPolicy, BoundaryPolicy
│   ├── consolidation/     # Online Consolidator + offline DreamEngine
│   ├── stores/            # FileStorage (JSONL+KV), InMemoryStorage
│   └── injection/         # MemoryInjectionPolicy → ContextState assembly
├── multi_agent/           # Multi-agent orchestration
│   └── inbox/             # MQ system: LocalFileInboxServer, Producer, Consumer, InboxFlushHook
├── plugins/               # Plugin system: MemoryProvider ABC, PluginContext, PluginManager
├── messaging/             # MessageBroker, BrokerBridgeService
├── extensions/            # Optional: LiteLLM provider, ChromaDB/FAISS stores, SQLAlchemy sessions
├── sandbox/               # Sandboxed execution: LocalPython, E2B, Docker, Subprocess adapters
├── security/              # SecurityPolicy, validators, approval handlers
└── utils/                 # MediaProcessor, tokenizer, helpers
```

## Example Project

`examples/bot_project/` is the primary end-to-end reference — a QQ Bot demonstrating all framework subsystems.

```
examples/bot_project/
├── bot_service.py         # BotService (pipeline/pool modes), SpawnSubagentTool
├── qq_adapters.py         # QQ platform InputAdapter/OutputAdapter/Emitter
├── plugin_integration.py  # PluginIntegration facade
├── config/bot_config.yml  # All-in-one config (LLM, memory, tools, multi_agent, plugins)
├── plugins/               # Project-local plugins (mem0_memory, tool_call_cleanup)
└── skills/{main,peers,subagents}/  # SKILL.md-based skill directories
```

Two runtime modes in `bot_service.py`:
- **Pipeline** (`mode="pipeline"`): Single `AgentPipeline`, SubagentManager creates direct `asyncio.Task`.
- **Pool** (`mode="pool"`): `AgentPool` manages resident agents via `BrokerBridgeService`, star-topology communication.

## Core Architectural Patterns

### 1. Generic Type-Safe Events

```python
class ReActEvent(AgentEvent, Enum):
    MODEL_OUTPUT = "model_output"
    TOOL_CALL_START = "tool_call_start"
    FINAL_OUTPUT = "final_output"
    ERROR = "error"

class ReActAgent(Agent[ReActEvent]):
    event_enum = ReActEvent
```

### 2. Two Usage Modes

- **AgentPipeline** — long-running services (QQ Bot, CLI). `await pipeline.run()` loops forever.
- **AgentSession** — HTTP API style (request/response). `await session.process_message(...)`.

### 3. Memory System

Three-layer with scope isolation: `Short-term → History → Long-term (SOUL/USER/MEMORY.md)`. Scopes: Session/User/Tenant/Agent/Channel/Chat/PeerPair/Composite/Global.

**Key abstractions (new redesign):**
- `MemoryCompactionPipeline` — unified entry for token-pressure and idle AutoCompact
- `MessageCompactionPolicy` — per-message decisions (KEEP_RAW/SUMMARIZE/DROP/ARCHIVE_RAW)
- `BoundaryPolicy` — tool-call chain safe truncation
- `SummaryStrategy` — LLM or heuristic summary generation
- `ScopeRecord` + `.scope.json` — recoverable scope metadata for background tasks

Compression is three-phase (pre-compress callbacks → strategy → hard limits), all tool-chain-aware. `DreamEngine` runs offline consolidation; `Consolidator` runs online LLM-based compression.

**Design documents:** `agent_docs/memory-system-redesign.md` and `agent_docs/memory-system-redesign-plan.md` describe the ongoing P0-P5 redesign.

### 4. Runtime Context System

Per-turn, per-session isolated state container for tracking tool calls and arbitrary runtime state.

```python
# Generic key-value state + tool-call tracking
class RuntimeContext(ABC):
    async def set(self, key: str, value: Any) -> None: ...
    async def get(self, key: str, default: Any = None) -> Any: ...
    async def record_tool_call(self, tool_name, arguments, result) -> None: ...
    async def get_tool_calls(self) -> list[ToolCallRecord]: ...

# Central manager using MemoryScope for session isolation (default SessionScope)
class RuntimeContextManager:
    async def get_context(self, session_id: str, metadata: dict | None = None) -> RuntimeContext: ...
```

- `ReActAgent` clears context at `before_turn`, records each tool call after execution
- `PeerAutoSendHook` queries context to detect whether a communication tool (`send_message` / `send_message_async`) was already called, avoiding duplicate auto-forward
- Layering: generic infrastructure in `core/runtime_context.py`, multi-agent logic in `multi_agent/hooks.py`

### 5. MCP Integration

`MCPClientManager` → `MCPToolAdapter` → `ToolRegistry`. Three transports: stdio, SSE, streamable_http. Auto-registers tools/resources/prompts as `Tool` objects with reconnection.

### 6. Multi-Agent: Star Topology

Peers communicate only through main agent (`PeerAgentValidator` enforces). `MessageBroker` for pub/sub, `AgentMessageEnvelope` for typed messages, `InboxServer` for async results. `PeerAutoSendHook` auto-forwards if LLM forgets `send_message_async`. `SubagentManager` supports spawn/spawn_and_wait/cancel.

### 7. Plugin System

Three discovery sources: bundled > user (`~/.af/plugins/`) > PyPI. Contract:

```python
def register(ctx: PluginContext) -> None:
    ctx.register_tool(...) / ctx.register_memory_provider(...) / ctx.register_hook(...)
```

Extension points: `MemoryProvider` (add/search/prefetch), Tools, Hooks, SkillSources, MemorySystem modifiers.

### 8. Skill System

`FileSkillSource` discovers `SKILL.md` from directories → `DependencyFilter` checks tool availability → `ProgressiveBuilder` resolves dependencies → `AgentSkillManager` wraps with per-agent white/deny list.

## Type Structuring Best Practices

1. **Enumerate constants**: All categories/states as `Enum` or `StrEnum`. No raw strings.
2. **Generic bindings**: `Agent[E]`, `ContentEmitter[E]` with `TypeVar("E", bound=AgentEvent)`.
3. **Dataclasses over dicts**: Use frozen dataclasses for config (e.g., `AgentDescriptor`, `MediaInfo`).
4. **Abstract early**: Every cross-cutting concern needs an ABC (Storage, Compression, Adapters, Skills).
5. **Config vs Runtime**: Config classes are pure data; Runtime classes hold state/connections.

## Type Safety Rules (from `.claude/rules/type-safety.md`)

1. **Use enums/constants over raw strings** (`MessageRole`, `MessageType`, `FinishReason`, etc.)
2. **Use structs over dicts** (`ChatMessage`, `ToolCall`, `LLMResponse`, etc.)
3. **Function signatures must have parameter and return types**. No bare `Any` / `list` / `dict` / `list[Any]`.
4. **Class design must abstract early**. Do not use concrete implementation classes directly; use abstract classes or interfaces for extensibility.

## Configuration

### Optional Dependencies

| Group | Dependencies | Use Case |
|-------|--------------|----------|
| `dev` | pytest, ruff, mypy | Development |
| `llm` | litellm | LLM support |
| `storage` | chromadb, faiss, sentence-transformers, aiosqlite | Vector memory |
| `session` | sqlalchemy[asyncio] | SQLAlchemy sessions |
| `sandbox` | docker, e2b-code-interpreter | Sandboxed execution |
| `gateway` | fastapi, qq-botpy, rich | Gateway adapters |

### Environment Variables

- `OPENAI_API_KEY` / `OPENAI_URL` / `OPENAI_MODEL` — LLM API
- `E2B_API_KEY` — E2B cloud sandbox
