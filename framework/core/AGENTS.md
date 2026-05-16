<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-05-16 | Updated: 2026-05-16 -->

# core

Abstract base classes and shared types forming the framework's type-safe foundation. All other sub-packages depend on these abstractions.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `Agent[E]` generic ABC, `AgentContext` dataclass, `current_agent_context` contextvar |
| `emitter.py` | `ContentEmitter[E]` ABC, `AgentResult`, `StreamingAwareEmitter`, `BufferingEmitter` |
| `events.py` | `AgentEvent` base, `EmitterConfig` — event filtering/truncation |
| `provider.py` | `LLMProvider` / `StreamingLLMProvider` ABCs with retry logic |
| `tool.py` | `Tool` class (dual-mode init), `DynamicSchemaProvider` protocol |
| `tool_manager.py` | `ToolManager` ABC, `InMemoryToolManager`, `ToolResult`, `FunctionalTool`, batch execution |
| `tool_call_accumulator.py` | Streaming tool-call chunk assembly (`AccumulatingToolCall`, `parse_tool_call_chunks_from_delta`) |
| `context.py` | `ContextManager` ABC, `InMemoryContextManager`, `EphemeralContextManager`, `FileContextManager` |
| `types.py` | `InputMessage`, `OutputMessage`, `LLMResponse`, `ToolCall`, `MessageRole`, `MessageType` |
| `constants.py` | `DefaultValues`, `FinishReason` |
| `llm_struct.py` | `LLMErrorInfo`, `LLMTimeoutPolicy`, `TurnTimeoutPolicy`, `RuntimeSafetyPolicy`, `LLMProviderConfig` |
| `agent_runtime_config.py` | `AgentRuntimeConfig`, `BusyInputMode` |
| `runtime_context.py` | `RuntimeContext` ABC, `RuntimeContextManager` — per-session state + tool-call tracking |
| `strategy.py` | `ExecutionStrategy` ABC, `ReActStrategy`, `SingleTurnStrategy` |
| `runner.py` | `InterruptibleRunner` — graceful cancellation wrapper |
| `message_utils.py` | Agent message normalization for LLM format |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `graph/` | Directed graph state machine — `Graph[R]`, `Node[R]`, `GraphEngine`, `GraphInterrupt` |
| `skills/` | `SkillManager`, `FileSkillSource`, `ProgressiveBuilder`, `DirectorySkillCache` |

## For AI Agents

### Working In This Directory
- New ABCs: `Protocol` or `ABC` + `@abstractmethod`
- `AgentContext` is a dataclass — new fields must have `None` defaults
- Event enums: `class MyEvent(AgentEvent, Enum)`
- `Tool` has dual-mode construction: pass args to `__init__` OR define `@property` name/description/parameters

### Testing
- `tests/unit/core/` — mock `LLMProvider`, never hit real APIs

### Common Patterns
- `TYPE_CHECKING` guard for import-only types
- `contextvars.ContextVar` for per-asyncio-task state
- `Tool.validate_params()` does recursive JSON-Schema-style validation
