<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-31 -->

# core

Abstract base classes and shared types forming the framework's type-safe foundation. All other sub-packages depend on these abstractions.

## Purpose

The `core/` module defines the foundational contracts (`Agent[E]`, `Tool`, `LLMProvider`, `ContextManager`, `ContentEmitter[E]`), type system (`MessageRole`, `MessageType`, `ToolCall`), runtime context (`AgentContext`), session identity (`SessionInfo`, `SessionRegistry`, `SessionStore`), and sub-systems (skills, experience). Every other framework module imports from `core/`. The graph engine was extracted to the standalone `modex_graph` package (ADR-0033); the old `core/graph/` directory is deleted.

## Key Files

| File | Description |
|------|-------------|
| `agent.py` | `Agent[E]` generic ABC — `run()` entry point, `event_enum`, tool/skill registration. `AgentContext` (dataclass, runtime state + services). `current_agent_context` ContextVar |
| `capabilities.py` | `Modality` (StrEnum: TEXT/IMAGE/VIDEO/AUDIO), `ModelCapabilities` (frozen Pydantic BaseModel: modalities + `supports()`), `ModelInfo` (frozen Pydantic BaseModel: `model_name` + `capabilities`). Moved from `ioc/configs/llm.py` so `core/tool_manager` and `runtime/services` can reference them without upward ioc dependency; `ioc/configs/llm.py` re-exports for config-layer consumers |
| `constants.py` | `DefaultValues`, `FinishReason` (`(str, Enum)` — pre-existing; new enums use `StrEnum`), `ToolCallType`, `ToolChoice`, `ErrorMessages`, `ToolSchemaConstants`, `StreamControlAction` (StrEnum, B1) |
| `context.py` | `ContextState` (StrEnum: ACTIVE/PAUSED/STOPPED), `ContextManager` ABC, `InMemoryContextManager` |
| `emitter.py` | `ContentEmitter[E]` ABC — event streaming contract. `AgentResult` (Pydantic `BaseModel`, ADR-0033 D14 Stage 2). `StreamingAwareEmitter` moved to `modex_agent/adapters/emitter.py` (B4) |
| `events.py` | `AgentEvent` base class/Enum mixin, `EmitterConfig` (filters, truncation, max_events) |
| `frontmatter.py` | Shared YAML frontmatter parsing (`parse_frontmatter()`) for markdown docs (skills, experiences) |
| `governance.py` | `GovernanceResult` — context governance result type |
| `history.py` | `MessageHistory` ABC — message append/filter/get |
| `llm_request.py` | `LLMRequest` (frozen Pydantic `BaseModel`, ADR-0046) — canonical request envelope for LLM calls: `model`, `messages`, `tools`, `temperature`, `top_p`, `max_output_tokens`, `stop`, `reasoning_effort`, `prompt_cache_key`, `extra_body`. The ONLY carrier of sampling parameters — HTTP headers carry auth/passthrough, never sampling knobs |
| `llm_struct.py` | `LLMErrorInfo` (Pydantic `BaseModel(frozen=True)`, ADR-0033 D14 Stage 2), `LLMErrorKind` (StrEnum), `ProviderKind` (StrEnum), `LLMTimeoutPolicy` (defaults `None` — no provider-level timeout), `TurnTimeoutPolicy` (`dispatch_timeout_seconds`=600s no-progress budget re-asserted at each LLM call entry, `tool_timeout_seconds`=540s), `DeadlinePolicy` (watchdog knobs: `chunk_renew_seconds`/`max_ahead_seconds`/`watchdog_poll_seconds`; derived phase margin = 2×poll), `RuntimeSafetyPolicy` (with a startup model validator: `max_ahead_seconds >= every phase budget + margin`), `LLMProviderConfig` — all config/policy types are now frozen Pydantic `BaseModel` (B4) |
| `media.py` | Media contracts (moved from `media/models.py` + the store ABC in C1, plan §14.1; ADR-0013): `Attachment` (frozen Pydantic — the direction-agnostic transcript record), `Kind` / `AttachmentLocator` (StrEnum), `MediaStore` ABC (backend-swappable inbound byte store — an explicit retained seam, ADR-0013 §6), `StoredFile` (frozen Pydantic listing snapshot), `StoredMediaKind` (StrEnum), `MediaRefCollisionError`. Concrete filesystem behavior stays in `media/store.py` (`LocalFileMediaStore`); MIME/gate/security stay in `media/` |
| `message.py` | `ChatMessage` (BaseModel, `extra="allow"`) — `role: MessageRole`, `content: str | list[ContentPart] | None`, `tool_calls: list[ToolCall] | None`. Reasoning replay declaration fields (ADR-0046): `reasoning_content`, `reasoning_signature`, `reasoning_item_id`, `reasoning_encrypted_content` — chain-of-thought state rides on declared fields, never on `model_extra`. `ContentFormat` (StrEnum), `ContentPart` discriminated union (`TextPart | ImageUrlPart`). `content_part_modality()` — the part→`Modality` authority (closed match; future part variants must extend it). `MEDIA_URL_SCHEME` + `build_media_ref`/`parse_media_ref` — `media://<attachment_id>` persistence refs. `render_content_part_ref` — shared bracket-line ref renderer (`[image: media://<aid>]` / `[image: data:<mime>, <n> bytes]`), the single semantics consumed by telemetry and memory renders — zero media bytes ever rendered. `to_dict()` serializes `tool_calls` to OpenAI wire format |
| `message_utils.py` | `normalize_agent_messages_for_llm()` — normalizes messages to LLM-compatible format |
| `prompt.py` | Prompt building utilities |
| `provider.py` | `LLMProvider` ABC (ADR-0046) — the event-stream ABC: abstract `stream(request)` (the single streaming primitive; every stream ends with exactly one `Finish`/`StreamFailure`) + `get_default_model()`; concrete `chat_stream()` (EventAssembler fold of the event stream into an `LLMResponse` with delta callbacks) and `chat()` (one internal retry over `chat_stream`). Callback-style implementations (cassette record/replay, delegation proxies, scripted test providers) subclass `CallbackStreamProvider` — its concrete `stream()` bridges `chat_stream()` back into the event stream |
| `scope.py` | Scope-related type definitions |
| `session_id.py` | `SessionInfo` (Pydantic BaseModel — the single identity object), `SessionIdFactory`, `now_ms()`, snowflake encoding (`encode_snowflake`/`session_id_prefix_of`/`agent_of`) |
| `session_registry.py` | `SessionRegistry` — async write-through cache over `SessionStore` for `SessionInfo` resolution (guarded by `asyncio.Lock`) |
| `session_store.py` | `SessionStore` ABC + JSON-file `LocalFileSessionStore` — authoritative persistent session storage (one JSON per session_id, I/O via `asyncio.to_thread`) |
| `stream_events.py` | `LLMStreamEvent` — six-variant closed discriminated union (ADR-0046): `TextDelta` / `ReasoningDelta` / `ToolCallComplete` / `UsageSnapshot` / `Finish` / `StreamFailure`. `ReplayFields` (`Finish.replay`) is the event layer's only reasoning-replay channel. `EventAssembler` (folded here from providers/http in B3) folds an event sequence into one `LLMResponse` and enforces the terminal-event invariant — every stream ends with exactly one `Finish` or `StreamFailure`, synthesized as a TIMEOUT failure on EOF without terminal |
| `tool.py` | `DynamicSchemaProvider` ABC — context-aware tool schema. **`Tool` class lives in `tool_manager.py`** |
| `tool_manager.py` | `Tool` class (dual-mode: `__init__` args OR `@property` name/description/parameters; declarative modalities per ADR-0014), `ExecutionMode`, `ParallelTool`, `ExclusiveTool`, `ToolManager` ABC, `InMemoryToolManager`, `ToolResult`, `ToolExecutionContext`, `ToolConfig`, `ToolManagerConfig`. `Tool.execution_mode` is a read-only property that resolves the instance `_execution_mode_override` before the fail-closed class `_default_execution_mode`; `Tool.on_cancel()` owns external-state recovery and `Tool.cancel_note` adds context to synthesized cancellation results. |
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

