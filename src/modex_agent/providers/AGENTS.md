<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-26 -->

# providers

## Purpose
LLM provider subsystem — a single system: the direct-HTTP event-stream subsystem `http/` (ADR-0046 — `HTTPStreamProvider` + three protocol engines), the ONLY provider implementation. The provider ABC lives in `core/provider.py` (`LLMProvider` event-stream ABC + `CallbackStreamProvider` callback adapter base); `create_llm_provider` routes every `InterfaceFormat` here. The legacy SDK providers and their `shared/` streaming utilities were removed (2026-08-26 cleanup).

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | Re-exports `HTTPStreamProvider` — the public provider class |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `http/` | Direct-HTTP event-stream subsystem (ADR-0046) — `HTTPStreamProvider` + three protocol engines; the default provider path via `create_llm_provider` |
| `http/sse.py` | SSE frame parsing (`SseFrame`) — data-only (OpenAI chat) and event+data (Responses, Anthropic) frame shapes; `[DONE]` sentinel passed through for the engine to own |
| `http/errors.py` | Default HTTP error classification (`classify_http_error`) — raw status + body → `LLMErrorInfo`, no SDK dependency |
| `http/tool_stream.py` | Generic streamed tool-call accumulator — keys accumulation on the stream key (block `index` / `item_id`), never on `call_id` |
| `http/protocol.py` | `LLMProtocol` ABC + `WireRequest`/`ProtocolConfig` envelopes — the contract between provider (transport) and engines (translation) |
| `http/provider.py` | `HTTPStreamProvider` — the one concrete direct-HTTP provider: owns the `httpx` client, request/response lifecycle, stream idle watchdog; takes the factory-resolved `url` and requests it verbatim; zero wire-format knowledge |
| `http/formats/` | Protocol engines — one module per wire format (ADR-0046) |
| `http/formats/openai_compat.py` | OpenAI Chat Completions compatible engine — data-only SSE, think-tag extraction, DeepSeek `reasoning_content` replay; tool media folds text into `tool` messages and flushes ONE attributed follow-up user message per contiguous tool run; unresolved `media://` refs ERROR+skip (permanent wire guard) |
| `http/formats/openai_responses.py` | OpenAI Responses API engine — event+data SSE, `item_id` stream key, `item_reference`(store=true opt-in)/`encrypted_content`(store=false default) reasoning replay, bare-replay-without-encrypted dropped, `prompt_cache_key` cache-routing passthrough; tool media embeds NATIVELY as `[input_text, input_image]` arrays inside `function_call_output.output` (no flush); unresolved `media://` refs ERROR+skip (permanent wire guard) |
| `http/formats/anthropic.py` | Anthropic Messages API engine — event+data SSE, thinking-block replay with signature, `x-api-key` auth, explicit prompt-cache breakpoints (system block + final block of the last two non-system messages, ephemeral); tool media embeds natively as image blocks inside the `tool_result` block; unresolved `media://` refs ERROR+skip (permanent wire guard) |

## For AI Agents
- `http/` (ADR-0046) is the sole provider subsystem: `create_llm_provider` routes all three `interface_format` values to `HTTPStreamProvider` wired with the matching protocol engine — there is no other provider implementation
- Consumers depend only on the `LLMProvider` ABC (`core/provider.py`) — event-stream implementations subclass `LLMProvider` (abstract `stream()`); response-level implementations (cassette record/replay, delegation proxies, scripted test providers) subclass `CallbackStreamProvider`
- `HTTPStreamProvider` accepts `list[ChatMessage]` (not `list[dict]`) per B6 LLM-message convergence — engines lower `ChatMessage` to wire dicts by explicit construction in `build_body`
- `HTTPStreamProvider` carries zero wire-format knowledge — provider owns transport, `LLMProtocol` engine owns translation; new wire formats are new engine files, never edits to the provider

## Dependencies
- `httpx` — for `HTTPStreamProvider` (direct-HTTP transport in `http/`)
- Consumed by `modex_agent/core/` (LLM abstraction layer) and `modex_agent/pipeline/`

<!-- MANUAL: -->
