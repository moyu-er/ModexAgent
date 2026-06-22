<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-06-22 -->

# core

Abstract base classes and shared types forming the framework's type-safe foundation. All other sub-packages depend on these abstractions.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `Agent[E]` generic ABC, `AgentContext` dataclass, `current_agent_context` ContextVar. Imports `SessionInfo` from `session_id.py`. |
| `emitter.py` | `ContentEmitter[E]` ABC, `AgentResult`, `StreamingAwareEmitter` |
| `events.py` | `AgentEvent` base, `EmitterConfig` — event filtering/truncation |
| `provider.py` | `LLMProvider` / `StreamingLLMProvider` ABCs |
| `tool.py` | `DynamicSchemaProvider` ABC (context-aware schema). `Tool` class lives in `tool_manager.py` |
| `tool_manager.py` | `Tool` class (dual-mode init), `ToolManager` ABC, `InMemoryToolManager`, `ToolResult`, `FunctionalTool`, `ToolConfig`, `ToolManagerConfig`, `ToolExecutionMode` |
| `tool_call_accumulator.py` | `ToolCallChunk`, `AccumulatingToolCall`, `ToolCallAccumulator`, `parse_tool_call_chunks_from_delta` |
| `context.py` | `ContextState`, `ContextManager` ABC, `InMemoryContextManager` |
| `types.py` | `InputMessage`, `OutputMessage`, `LLMResponse`, `ToolCall`, `MessageRole`(StrEnum), `MessageType`(Enum) |
| `constants.py` | `DefaultValues`, `FinishReason`, `ToolCallType`, `ToolChoice`, `ErrorMessages`, `ToolSchemaConstants` |
| `llm_struct.py` | `LLMErrorInfo`, `LLMErrorKind`, `ProviderKind`, `LLMTimeoutPolicy`, `TurnTimeoutPolicy`, `RuntimeSafetyPolicy`, `LLMProviderConfig` |
| `agent_runtime_config.py` | `AgentRuntimeConfig`, `BusyInputMode`, `RuntimeControl` |
| `runtime_context.py` | `ToolCallRecord`, `RuntimeContext` ABC, `InMemoryRuntimeContext`, `RuntimeContextStore` ABC, `InMemoryRuntimeContextStore`, `RuntimeContextManager` |

| `message_utils.py` | `normalize_agent_messages_for_llm` — message normalization for LLM format |
| `session_id.py` | `SessionInfo` (pydantic BaseModel, the single identity object), `SessionIdFactory`, `DefaultSessionIdStrategy`, `now_ms`, snowflake encoding (`encode_snowflake`/`session_id_prefix_of`/`agent_of`). **`SessionInfo` lives here, not in `agent.py`.** |
| `session_registry.py` | `SessionRegistry` — async write-through cache over `SessionStore` for SessionInfo resolution (guarded by asyncio.Lock). |
| `session_store.py` | `SessionStore` ABC + JSON-file implementation — authoritative persistent session storage (one JSON per session_id; I/O via `asyncio.to_thread`). |
| `frontmatter.py` | Shared YAML frontmatter parsing for markdown docs (skills, experiences). |

## Subdirectories

| Directory | Purpose |
|-----------|---------|
| `graph/` | Directed graph state machine — `Graph[R]`, `Node[R]`, `Edge`, `GraphEngine`, `GraphInterrupt`, `GraphNode`(StrEnum) |
| `skills/` | `SkillManager`, `SkillSource` ABC, `FileSkillSource`, `InlineSkillSource`, `CompositeSkillSource`, `SkillPromptBuilder` + `ProgressiveBuilder`/`HybridBuilder`/`InlineBuilder`, `DirectorySkillCache`, `SkillFilter` hierarchy |
| `experience/` | Experience layer — `ExperienceManager`, `FileExperienceSource`, `ExperiencePromptBuilder`, `ExperienceCurator`, validation, metadata tracking (see `experience/AGENTS.md`) |

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
- `contextvars.ContextVar` for per-asyncio-task state (`current_agent_context`)
- `SessionInfo` fields are authoritative; the string form is opaque — never parse it except via `session_id.py` helpers or `SessionInfo.from_str`.
