<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-30 -->

# core

## Purpose
Abstract base classes and shared types forming the framework's type-safe foundation. All other sub-packages depend on these abstractions.

## Key Files
| File | Description |
|------|-------------|
| `agent.py` | `Agent[E]` generic ABC, `AgentContext` dataclass, `current_agent_context` contextvar |
| `emitter.py` | `ContentEmitter[E]`, `AgentResult`, `StreamingAwareEmitter` |
| `events.py` | `AgentEvent` base enum — typed event system for emitter/agent binding |
| `provider.py` | `LLMProvider` / `StreamingLLMProvider` protocols |
| `tool.py` | `Tool` ABC — pluggable tool interface |
| `tool_manager.py` | `ToolManager` ABC, `InMemoryToolManager`, `ToolResult` |
| `context.py` | `ContextManager`, `InMemoryContextManager`, `EphemeralContextManager` |
| `types.py` | `InputMessage`, `LLMResponse`, `ToolCall`, `MessageRole` shared types |
| `constants.py` | `DefaultValues`, `FinishReason` enums/constants |
| `agent_runtime_config.py` | `AgentRuntimeConfig`, `RuntimeControl`, `BusyInputMode` — runtime wiring |
| `runtime_context.py` | `RuntimeContext`, `RuntimeContextManager` — per-session state container |
| `llm_error.py` | `RuntimeSafetyPolicy` — timeout/circuit-breaker safety |
| `strategy.py` | Strategy-related base types |
| `session.py` | Session-related base types |
| `message_utils.py` | Agent message normalization for LLM format |
| `tool_call_accumulator.py` | Streaming tool call accumulation |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `skills/` | Skill system — `SkillManager`, `FileSkillSource`, `ProgressiveBuilder` |

## For AI Agents

### Working In This Directory
- Every new ABC must use `Protocol` or `ABC` + `@abstractmethod`
- `AgentContext` is a `@dataclass` with `field(default_factory=...)` for mutable defaults
- New fields on `AgentContext` must have sensible `None` defaults — many consumers use minimal contexts
- Event enums inherit `AgentEvent` + `Enum`

### Testing Requirements
- Tests in `tests/unit/core/`
- Mock `LLMProvider` rather than hitting real APIs

### Common Patterns
- `TYPE_CHECKING` guard for import-only types
- Generic: `class Agent(ABC, Generic[E])` with `TypeVar('E', bound=AgentEvent)`
- `contextvars.ContextVar` for per-asyncio-task state

## Dependencies

### Internal
- `framework.memory` — `ChatMessage`, `MessageHistory`, `ContextGovernance`

### External
- None (no external runtime deps at the core level)
## Current Runtime Status

Core graph primitives support durable ReAct macro steps, but hook/interceptor/control
should remain layered runtime services. Do not model low-latency control only as
ordinary graph nodes. See `docs/current-runtime.md`.
