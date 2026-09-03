<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-09-02 -->

# core

Abstract base classes and shared types forming the framework's type-safe foundation. All other sub-packages depend on these abstractions.

## Purpose

The `core/` module defines foundational contracts and values used directly across the framework: agents, emitters, canonical messages, LLM requests/results, tools, media, `MessageHistory`, system-prompt seams, session identity, and `RecordScope`. Concrete memory/context behavior lives in `memory/`; session stores and registries live in `persistence/`. The graph engine is the standalone `modex_graph` package (ADR-0033).

## Key Files

| File | Description |
|------|-------------|
| `__init__.py` | Exact foundational facade; concrete implementations are not re-exported. |
| `agent.py` | `Agent[E]`, `AgentContext`, agent identity/implementation enums, and `current_agent_context`. |
| `capabilities.py` | `Modality`, `ModelCapabilities`, and `ModelInfo`. |
| `emitter.py` | `ContentEmitter[E]`, `AgentResult`, and `StopReason`; concrete emitter behavior lives in `adapters/`. |
| `events.py` | `AgentEvent` and `EmitterConfig`. |
| `history.py` | `MessageHistory` ABC, the async history seam used directly by `AgentContext`; concrete histories live in `memory/history.py`. |
| `llm_request.py` | Canonical `LLMRequest` and `ReasoningEffort`. |
| `llm_struct.py` | LLM response, usage, error, finish, timeout, and runtime-safety values. |
| `media.py` | Attachment values and the `MediaStore` contract; filesystem behavior lives in `media/`. |
| `message.py` | Canonical `ChatMessage`, `MessageRole`, `ToolCall`, content parts, and media-reference helpers. |
| `message_utils.py` | Shared LLM normalization and system-reminder helpers. |
| `prompt.py` | `SystemPromptProvider` and `SystemPromptPipeline`, the prompt seams used directly by `AgentContext`; provider implementations live with consumers. |
| `provider.py` | `LLMProvider` and `CallbackStreamProvider`. |
| `scope.py` | Canonical `RecordScope` identity only; configurable memory scopes live in `memory/scope.py`. |
| `session_id.py` | `SessionInfo`, `SessionIdFactory`, and identity encoding/parsing helpers; persistence lives in `persistence/`. |
| `stream_events.py` | Closed LLM stream-event union and `EventAssembler`. |
| `tool_manager.py` | Tool/manager contracts, execution values, and shared execution behavior; `InMemoryToolManager` lives in `tools/manager.py`. |
| `turn_events.py` | Provider-neutral semantic `TurnEvent` variants. |

## For AI Agents

### Working In This Directory
- New pluggable contracts use `ABC` + `@abstractmethod`, not `Protocol`.
- `AgentContext` is a dataclass — new fields must have `None` defaults
- Event enums: `class MyEvent(AgentEvent, Enum)`
- **`Tool` is in `tool_manager.py` (not `tool.py` — deleted in C2)**. Dual-mode: pass args to `__init__` OR define `@property` name/description/parameters
- `from __future__ import annotations` in all modules

### Type Safety
- `Agent[E]`, `ContentEmitter[E]` with `TypeVar("E", bound=AgentEvent)`
- Enums/constants over raw strings — `MessageRole`, `AgentRole`, `FinishReason`, `StopReason`
- Typed structures over loose dicts — `ChatMessage`, `ToolCall`, `LLMResponse`
- Typed signatures — no bare `Any`, `list`, `dict`, `object` in framework-facing APIs
- No dynamic access (`getattr`/`hasattr`) except at real extension boundaries

### Common Patterns
- `TYPE_CHECKING` guard for import-only types
- `contextvars.ContextVar` for per-asyncio-task state (`current_agent_context`)
- `SessionInfo` fields are authoritative; the string form is opaque — never parse it except via `session_id.py` helpers or `SessionInfo.from_str()`
- `from __future__ import annotations` enables PEP 604 union syntax

### Testing
- `tests/unit/core/` — mock `LLMProvider`, never hit real APIs
- Absolute imports (`from modex_agent.core.xxx`)

## Dependencies

### Internal
- Depends only on shared `modex_agent.utils` helpers; it has no upward feature-package imports.

### External
- `pydantic` — `SessionInfo`, `ChatMessage` models

<!-- MANUAL -->
<!-- Additional manual entries can be added below this line. -->
