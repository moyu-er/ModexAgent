<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 | Updated: 2026-06-22 -->

# shared

## Purpose

Shared utility types and functions used by LLM provider implementations. Provides streaming delta normalization, non-streaming response parsing, and structured error classification for the OpenAI SDK. These are imported directly by provider modules and are not re-exported from `framework/providers/__init__.py`.

## Key Files

| File | Description |
|------|-------------|
| `delta.py` | `StreamDelta` dataclass — structured extraction from streaming chunks (content, reasoning_content, tool_call_chunks, finish_reason); `ParsedResponse` dataclass — non-streaming response extraction; `extract_reasoning()` — extracts `reasoning_content` from Pydantic `model_extra` |
| `errors.py` | `classify_openai_error(exc)` — isinstance-based error classification mapping openai SDK exceptions to `LLMErrorInfo` with kind, should_retry, and status_code; supports `APITimeoutError`, `APIConnectionError`, `InternalServerError`, `RateLimitError`, `AuthenticationError`, `PermissionDeniedError`, `BadRequestError` |
| `__init__.py` | Module docstring documenting the exported types |

## For AI Agents

### Working In This Directory
- `StreamDelta.from_openai(delta)` converts openai SDK `ChoiceDelta` objects to framework-internal streaming chunks — this is the canonical way to normalize streaming responses across providers
- `classify_openai_error` uses strict `isinstance` checks with a string-scan fallback for non-openai exceptions; no `getattr`/`hasattr` reflection
- `extract_reasoning()` uses Pydantic's official `model_extra` mechanism to extract `reasoning_content` — this is NOT the same as `getattr(model, "reasoning_content")` and is the correct approach for Pydantic v2
- These utilities are consumed by `LiteLLMProvider` and `OpenAIProvider` but also available for any custom provider implementation

### Common Patterns
- All types are frozen dataclasses with typed fields — no dict-based intermediate representations
- Error classification returns `LLMErrorInfo(LLMErrorKind, message, service, should_retry, status_code)` — caller checks `should_retry` to decide retry logic
- `ToolCallChunk` (from `framework.core.tool_call_accumulator`) is used for incremental tool call building during streaming

## Dependencies

### Internal
- `framework/core/tool_call_accumulator.py` — `ToolCallChunk` for streaming tool call accumulation
- `framework/core/types.py` — `ToolCall` for parsed tool calls
- `framework/core/llm_struct.py` — `LLMErrorInfo`, `LLMErrorKind` for structured error data

### External
- `openai` (optional) — SDK exception types used by `errors.py`; gracefully handled via `try/except ImportError`

<!-- MANUAL -->
