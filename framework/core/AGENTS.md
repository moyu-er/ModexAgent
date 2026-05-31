<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-05-31 | Branch: develop_gyt -->

# core

Abstract base classes and shared types forming the framework's type-safe foundation. All other sub-packages depend on these abstractions.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `Agent[E]` generic ABC, `AgentContext` dataclass, `AgentSessionMeta`, `current_agent_context` ContextVar |
| `emitter.py` | `ContentEmitter[E]` ABC, `AgentResult`, `StreamingAwareEmitter`, `BufferingEmitter`, `LoggingEmitter` |
| `events.py` | `AgentEvent` base, `EmitterConfig` — event filtering/truncation |
| `provider.py` | `LLMProvider` / `StreamingLLMProvider` ABCs |
| `tool.py` | `DynamicSchemaProvider` protocol (context-aware schema). `Tool` class lives in `tool_manager.py` |
| `tool_manager.py` | `Tool` class (dual-mode init), `ToolManager` ABC, `InMemoryToolManager`, `ToolResult`, `FunctionalTool`, `ToolConfig`, `ToolManagerConfig`, `ToolExecutionMode` |
| `tool_call_accumulator.py` | `ToolCallChunk`, `AccumulatingToolCall`, `ToolCallAccumulator`, `parse_tool_call_chunks_from_delta` |
| `context.py` | `ContextState`, `ContextManager` ABC, `InMemoryContextManager`, `EphemeralContextManager`, `FileContextManager` |
| `types.py` | `InputMessage`, `OutputMessage`, `LLMResponse`, `ToolCall`, `MessageRole`(StrEnum), `MessageType`(Enum) |
| `constants.py` | `DefaultValues`, `FinishReason`, `ToolCallType`, `ToolChoice`, `ErrorMessages`, `ToolSchemaConstants` |
| `llm_struct.py` | `LLMErrorInfo`, `LLMErrorKind`, `ProviderKind`, `LLMTimeoutPolicy`, `TurnTimeoutPolicy`, `RuntimeSafetyPolicy`, `LLMProviderConfig` |
| `agent_runtime_config.py` | `AgentRuntimeConfig`, `BusyInputMode`, `RuntimeControl` |
| `runtime_context.py` | `ToolCallRecord`, `RuntimeContext` ABC, `InMemoryRuntimeContext`, `RuntimeContextStore` ABC, `InMemoryRuntimeContextStore`, `RuntimeContextManager` |
| `strategy.py` | `ExecutionStrategy` ABC, `ReActStrategy`, `SingleTurnStrategy` |
| `runner.py` | `InterruptibleRunner` — graceful cancellation wrapper |
| `message_utils.py` | `normalize_agent_messages_for_llm` — message normalization for LLM format |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `graph/` | Directed graph state machine — `Graph[R]`, `Node[R]`, `Edge`, `GraphEngine`, `GraphInterrupt`, `GraphNode`(StrEnum) |
| `skills/` | `SkillManager`, `SkillSource` ABC, `FileSkillSource`, `InlineSkillSource`, `CompositeSkillSource`, `SkillPromptBuilder` + `ProgressiveBuilder`/`HybridBuilder`/`InlineBuilder`, `DirectorySkillCache`, `SkillFilter` hierarchy |

## For AI Agents

### Working In This Directory
- New ABCs: `Protocol` or `ABC` + `@abstractmethod`
- `AgentContext` is a dataclass — new fields must have `None` defaults
- Event enums: `class MyEvent(AgentEvent, Enum)`
- `Tool` is in `tool_manager.py` (not `tool.py`). Dual-mode: pass args to `__init__` OR define `@property` name/description/parameters

### Testing
- `tests/unit/core/` — mock `LLMProvider`, never hit real APIs

### Common Patterns
- `TYPE_CHECKING` guard for import-only types
- `contextvars.ContextVar` for per-asyncio-task state (`current_agent_context`, `current_conversation_id`)
