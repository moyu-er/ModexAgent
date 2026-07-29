<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# core

Abstract base classes and shared types forming the framework's type-safe foundation. All other sub-packages depend on these abstractions.

## Purpose

The `core/` module defines the foundational contracts (`Agent[E]`, `Tool`, `LLMProvider`, `ContextManager`, `ContentEmitter[E]`), type system (`MessageRole`, `MessageType`, `ToolCall`), runtime context (`AgentContext`, `RuntimeContext`), session identity (`SessionInfo`, `SessionRegistry`, `SessionStore`), and sub-systems (skills, experience). Every other framework module imports from `core/`. The graph engine was extracted to the standalone `modex_graph` package (ADR-0033); the old `core/graph/` directory is deleted.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `Agent[E]` generic ABC — `run()` entry point, `event_enum`, tool/skill registration. `AgentContext` (dataclass, runtime state + services). `current_agent_context` ContextVar |
| `agent_runtime_config.py` | `BusyInputMode` — agent busy-input handling mode (the former `RuntimeControl`/`AgentRuntimeConfig` aggregates were dead and removed in ④b) |
| `capabilities.py` | `Modality` (StrEnum: TEXT/IMAGE/VIDEO/AUDIO), `ModelCapabilities` (frozen Pydantic BaseModel: modalities + `supports()`), `ModelInfo` (frozen Pydantic BaseModel: `model_name` + `capabilities`). Moved from `ioc/configs/llm.py` so `core/tool_manager` and `runtime/services` can reference them without upward ioc dependency; `ioc/configs/llm.py` re-exports for config-layer consumers |
| `constants.py` | `DefaultValues`, `FinishReason` (`(str, Enum)` — pre-existing; new enums use `StrEnum`), `ToolCallType`, `ToolChoice`, `ErrorMessages`, `ToolSchemaConstants`, `StreamControlAction` (StrEnum, B1) |
| `context.py` | `ContextState` (StrEnum: ACTIVE/PAUSED/STOPPED), `ContextManager` ABC, `InMemoryContextManager` |
| `emitter.py` | `ContentEmitter[E]` ABC — event streaming contract. `AgentResult` (Pydantic `BaseModel`, ADR-0033 D14 Stage 2). `StreamingAwareEmitter` ABC |
| `events.py` | `AgentEvent` base class/Enum mixin, `EmitterConfig` (filters, truncation, max_events) |
| `frontmatter.py` | Shared YAML frontmatter parsing (`parse_frontmatter()`) for markdown docs (skills, experiences) |
| `governance.py` | `GovernanceResult` — context governance result type |
| `history.py` | `MessageHistory` ABC — message append/filter/get |
| `llm_struct.py` | `LLMErrorInfo` (Pydantic `BaseModel(frozen=True)`, ADR-0033 D14 Stage 2), `LLMErrorKind` (StrEnum), `ProviderKind` (StrEnum), `LLMTimeoutPolicy`, `TurnTimeoutPolicy`, `RuntimeSafetyPolicy`, `LLMProviderConfig` — all config/policy types are now frozen Pydantic `BaseModel` (B4) |
| `message.py` | `ChatMessage` (BaseModel, `extra="allow"`) — `role: MessageRole`, `content: str | list[ContentPart] | None`, `tool_calls: list[ToolCall] | None`. `ContentFormat` (StrEnum), `ContentPart` discriminated union (`TextPart | ImageUrlPart`). `to_dict()` serializes `tool_calls` to OpenAI wire format |
| `message_utils.py` | `normalize_agent_messages_for_llm()` — normalizes messages to LLM-compatible format |
| `prompt.py` | Prompt building utilities |
| `provider.py` | `LLMProvider` ABC — `complete()`, `complete_streaming()`. `StreamingLLMProvider` ABC — streaming-specific contract |
| `runtime_context.py` | `ToolCallRecord` (dataclass), `RuntimeContext` ABC, `InMemoryRuntimeContext`, `RuntimeContextStore` ABC, `InMemoryRuntimeContextStore`, `RuntimeContextManager` |
| `scope.py` | Scope-related type definitions |
| `session_id.py` | `SessionInfo` (Pydantic BaseModel — the single identity object), `SessionIdFactory`, `DefaultSessionIdStrategy`, `now_ms()`, snowflake encoding (`encode_snowflake`/`session_id_prefix_of`/`agent_of`/`conv_id_of`) |
| `session_registry.py` | `SessionRegistry` — async write-through cache over `SessionStore` for `SessionInfo` resolution (guarded by `asyncio.Lock`) |
| `session_store.py` | `SessionStore` ABC + JSON-file `LocalFileSessionStore` — authoritative persistent session storage (one JSON per session_id, I/O via `asyncio.to_thread`) |
| `tool.py` | `DynamicSchemaProvider` ABC — context-aware tool schema. **`Tool` class lives in `tool_manager.py`** |
| `tool_call_accumulator.py` | `ToolCallChunk`, `AccumulatingToolCall`, `ToolCallAccumulator`, `parse_tool_call_chunks_from_delta()` — streaming tool-call accumulation |
| `tool_manager.py` | `Tool` class (dual-mode: `__init__` args OR `@property` name/description/parameters; declarative `required_modalities`/`produced_modalities` + `is_available(caps)` per ADR-0014), `ToolManager` ABC, `InMemoryToolManager`, `ToolResult` (`content: list[ContentPart]` is the source of truth; `image_blocks`/`content_blocks` are @computed_field for multimodal consumers), `ToolExecutionContext` (frozen BaseModel delivered via contextvar — carries `ModelInfo` to tools), `get_tool_execution_context()`, `FunctionalTool`, `ToolConfig`, `ToolManagerConfig`, `ToolExecutionMode` |
| `types.py` | `InputMessage`, `OutputMessage` (frozen Pydantic `BaseModel`, B5), `LLMResponse`, `ToolCall` (Pydantic `BaseModel`, ADR-0033 D14 Stage 2), `MessageRole` (StrEnum: SYSTEM/USER/ASSISTANT/TOOL/AGENT/PENDING), `MessageType` (Enum), `OutputMessageType` (StrEnum, B1) |
| `utils.py` | Core utility helpers |

## Subdirectories

| Directory | Files | Purpose |
|-----------|-------|---------|
| `skills/` | 7 py | Skill loading, filtering, caching, progressive prompt building — `SkillManager`, `SkillSource` ABCs, `SkillPromptBuilder`, `SkillFilter` hierarchy (see `skills/AGENTS.md`) |
| `experience/` | 10 py | Experience layer — `ExperienceManager`, `FileExperienceSource`, `ExperiencePromptBuilder`, `ExperienceCurator`, validation, metadata tracking (see `experience/AGENTS.md`) |

## For AI Agents

### Working In This Directory
- New ABCs: `Protocol` or `ABC` + `@abstractmethod`
- `AgentContext` is a dataclass — new fields must have `None` defaults
- Event enums: `class MyEvent(AgentEvent, Enum)`
- **`Tool` is in `tool_manager.py` (not `tool.py`)**. Dual-mode: pass args to `__init__` OR define `@property` name/description/parameters
- `from __future__ import annotations` in all modules

### Type Safety
- `Agent[E]`, `ContentEmitter[E]` with `TypeVar("E", bound=AgentEvent)`
- Enums/constants over raw strings — `MessageRole`, `MessageType`, `FinishReason`, `DefaultValues`
- Typed structures over loose dicts — `ChatMessage`, `ToolCall`, `LLMResponse`, `InputMessage`, `OutputMessage`
- Typed signatures — no bare `Any`, `list`, `dict`, `object` in framework-facing APIs
- No dynamic access (`getattr`/`hasattr`) except at real extension boundaries

### Common Patterns
- `TYPE_CHECKING` guard for import-only types
- `contextvars.ContextVar` for per-asyncio-task state (`current_agent_context`)
- `SessionInfo` fields are authoritative; the string form is opaque — never parse it except via `session_id.py` helpers or `SessionInfo.from_str()`
- `from __future__ import annotations` enables PEP 604 union syntax

### Testing
- `tests/unit/core/` — mock `LLPprovider`, never hit real APIs
- Absolute imports (`from modex_agent.core.xxx`)

## Dependencies

### Internal
- No internal framework dependencies (foundational module).

### External
- `pydantic` — `SessionInfo`, `ChatMessage` models
- `pyyaml` (optional) — frontmatter parsing in `frontmatter.py`
- `pathvalidate` — filename sanitization (used by skills)

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->

