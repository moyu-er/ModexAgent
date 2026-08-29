# LLM Provider Protocol Engines

Status: accepted. This PRD archives the settled design of ADR-0046 and is the specification source for the implementation work plan. It records decisions already made; it does not open new ones.

Related: ADR-0046 (`docs/adr/0046-llm-provider-protocol-engines.md`); `CONTEXT.md` terms "Canonical Message", "Wire Message", "Protocol Engine", "Event Stream", "Reasoning Replay", "Tool Stream Key".

Naming: the engine modules are `openai_compat`, `openai_responses`, `anthropic`. The `InterfaceFormat` config values that route to them are `openai_compatible`, `openai_response`, `anthropic`. This PRD uses engine names for code and config values for configuration.

## 1. Architecture: Layering and the Two Disciplines

The rebuild replaces both legacy SDK providers (native OpenAI SDK and litellm) with one concrete provider, three protocol engines, and a provider-neutral event stream.

```
LLMProvider                          THE public ABC (event-stream primitive)
    |                                 chat_stream() call face unchanged
    |                                 (86 call sites, 40+ mocks untouched)
    |   stream(request): abstract     THE single streaming primitive; every
    |                                 stream ends with exactly one
    |                                 Finish / StreamFailure
    |   chat_stream(): concrete       fold of stream() through EventAssembler
    |                                 with delta callbacks into one LLMResponse
    |   chat(): concrete              chat_stream + one internal retry
    |                                 (no public retry entry point)
    |
CallbackStreamProvider              adapter base (subclass of LLMProvider)
    |   chat_stream(): abstract      response-level implementations override
    |                                 this and keep working (cassette
    |                                 record/replay, delegation proxies,
    |                                 scripted test providers)
    |   stream(): concrete bridge     callbacks re-emitted as events, so the
    |                                 40+ chat_stream-only mocks gain an event
    |                                 view with zero migration
    |
HTTPStreamProvider                  the one concrete provider
        httpx.AsyncClient, SSE framing, idle watchdog,
        HTTP error classification, header merge, URL resolution
          |
          |  engine injected at construction; the format is never re-checked
          v
LLMProtocol (ABC)  <---  openai_compat  |  openai_responses  |  anthropic
```

The shipped form collapsed the migration's two-tier ABC (an event-stream tier above the legacy callback tier) into this single shape: `LLMProvider` is the event-stream ABC itself, and `CallbackStreamProvider` is the adapter base for response-level implementations. The intermediate tier existed only as the migration stepping stone.

Routing happens exactly once: `create_llm_provider` maps the `InterfaceFormat` to an engine instance (`openai_compatible` to OpenAICompatProtocol, `openai_response` to OpenAIResponsesProtocol, `anthropic` to AnthropicProtocol) and injects it into `HTTPStreamProvider`. The `multi_agent` fallback provider goes through the same factory. Everything below the factory talks only to the `LLMProtocol` ABC.

Two disciplines hold the design together:

1. **`provider.py` contains zero `if format ==` branches.** The provider owns transport only. Per-format behavior (which fields exist, where system goes, how a tool result is shaped) lives inside each engine's `build_body` and `events`. Adding a format means adding an engine, never editing the provider.
2. **Engines are stateless across requests.** `build_body` is a pure function of `(LLMRequest, ProtocolConfig)`. `events(frames)` returns a fresh async generator per request whose closure holds all translation state (tool stream state, usage buffers, signature caches). Replay state leaves the engine only through `Finish.replay` (see chapter 9); engines expose no per-response instance methods or attributes.

Engine files are structurally self-similar, with a fixed section order: common inputs, wire request schema, parse state, body building, event parsing, exports. A future Gemini engine is a new engine file following that shape, not a revived legacy provider.

`LLMProtocol` defines four abstract operations, one concrete default, and one property:

| Member | Responsibility |
|--------|----------------|
| `build_body(request, cfg) -> dict` | Explicit construction of the wire body from the canonical model |
| `url(base_url) -> str` | Per-format URL join |
| `auth_headers(api_key) -> dict` | Engine auth headers only (merged with user headers upstream) |
| `events(frames) -> AsyncIterator[LLMStreamEvent]` | Stream-to-event translation; fresh generator per request |
| `classify_http_error(status, body, headers)` | Concrete default (the generic classifier); engines may override |
| `api_key_env -> str` | Environment-variable fallback name (`OPENAI_API_KEY` for both OpenAI formats, `ANTHROPIC_API_KEY` for anthropic) |

## 2. Core Vocabulary: SseFrame, LLMStreamEvent, TokenUsage, LLMRequest

Four struct layers carry every request from bytes to typed response.

### 2.1 SseFrame (transport layer)

```python
@dataclass(frozen=True)
class SseFrame:
    event: str | None   # None for data-only protocols (chat completions)
    data: str
```

`sse_frames(byte_stream, on_activity=None) -> AsyncIterator[SseFrame]` is a hand-written line parser with no third-party dependency:

- `data:` and `event:` lines; multi-line data joined with `\n`
- `\n` and `\r\n` line endings; BOM tolerated
- incremental UTF-8 decoding, so a byte chunk may split inside a multi-byte sequence
- comment lines (leading `:`) produce no frame; they only trigger `on_activity`
- a blank line dispatches the accumulated frame; every frame emission calls `on_activity` first (this re-arms the idle watchdog, chapter 6)
- `DONE_SENTINEL = "[DONE]"`: when the data payload is exactly `[DONE]` the parser emits the frame as-is; engines special-case the sentinel
- tolerance for 200-with-non-SSE JSON bodies: when the first complete chunk cannot be parsed as SSE (no `data:`/`event:` line, whole body is valid JSON), the entire body is yielded as a single frame and the caller's error classification handles it

### 2.2 LLMStreamEvent (event layer, closed union of six variants)

```python
class ReplayFields(BaseModel):          # frozen, extra="forbid"
    reasoning_content: str | None = None
    reasoning_signature: str | None = None
    reasoning_item_id: str | None = None
    reasoning_encrypted_content: str | None = None

LLMStreamEvent:                        # Annotated union, discriminator "kind",
                                       # frozen, extra="forbid"
    TextDelta(text: str)                                       # kind="text_delta"
    ReasoningDelta(text: str)                                  # kind="reasoning_delta"
    ToolCallComplete(call_id: str, tool_name: str, arguments: dict)
    UsageSnapshot(usage: TokenUsage)
    Finish(finish_reason: FinishReason, replay: ReplayFields | None = None)
    StreamFailure(error_info: LLMErrorInfo, partial_content: str = "")
```

Rules:

- The union is closed. Variants are added, never modified.
- Terminal invariant: every stream ends with exactly one `Finish` or one `StreamFailure`. The EventAssembler enforces it (chapter 6, discipline 3).
- V2 extension slot, reserved as a comment in the source and deliberately not implemented: `ToolCallDelta(kind="tool_call_delta", call_id, tool_name, args_fragment: str)`, streaming tool arguments out to consumers. Precondition: a real consumer exists (for example a WebUI tool panel rendering arguments live). Consumers dispatch with `match` plus an explicit ignore branch, so an unknown variant stays visible instead of silently vanishing.

`EventAssembler` folds events into one `LLMResponse`: `TextDelta`/`ReasoningDelta` accumulate and fire the delta callbacks; `ToolCallComplete` appends; `UsageSnapshot` records; `Finish` stores the terminal state and the replay payload; `StreamFailure` stores the error and splices `partial_content` in front of the accumulated content. `result()` applies the terminal invariant, stamps `completion_start_time` from the first event, and when `Finish.replay` is present the replay values win over accumulated values for the reasoning fields.

### 2.3 TokenUsage (usage layer)

```python
class TokenUsage(BaseModel):            # frozen, extra="forbid"
    input_tokens: int = 0                # uncached input
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0            # informational; already inside output_tokens

    @computed_field
    def total_tokens(self) -> int: ...   # input + cache_read + cache_creation + output
```

Disjoint counts, computed total. `total_tokens` is never taken from the wire. A `model_validator(mode="before")` normalizes the three vendors' key shapes plus the legacy cassette key shape:

- `input_tokens` present: used verbatim (Anthropic semantics)
- otherwise `prompt_tokens` present: `input_tokens = prompt_tokens - cache_read_input_tokens` (OpenAI/DeepSeek convention: prompt includes cached tokens)
- `completion_tokens` maps to `output_tokens`
- `prompt_tokens_details.cached_tokens`, `cache_read_input_tokens`, `prompt_cache_hit_tokens` map to the cache-read count
- `cache_creation_input_tokens`, `cache_creation.ephemeral_*` map to the cache-creation count
- `completion_tokens_details.reasoning_tokens` maps to the reasoning count
- unknown keys are ignored
- a normalization that would produce a negative count raises `ValueError` (for example `prompt_tokens=100` with `cache_read_input_tokens=150`); it never silently yields a negative number

`LLMResponse.usage` becomes `TokenUsage` with a default factory of an all-zero instance.

### 2.4 LLMRequest (request layer, sampling envelope)

```python
class LLMRequest(BaseModel):            # frozen, extra="forbid"
    model: str
    messages: list[ChatMessage]
    tools: tuple[dict, ...] = ()
    temperature: float | None
    top_p: float | None
    max_output_tokens: int | None
    stop: tuple[str, ...] | None
    reasoning_effort: ReasoningEffort = ReasoningEffort.NONE
    prompt_cache_key: str | None
    extra_body: dict[str, Any] | None
```

The sampling envelope is the only carrier of sampling parameters; HTTP headers carry only auth and user-configured passthrough (chapter 8). The model is serializable (`model_dump()` round-trip; future cassette key material). `extra_body` is the providerOptions-style escape hatch: merged into the body top level with user-wins precedence.

## 3. Wire-Mapping Master Table (Three Engines)

How every canonical field lowers onto each wire. Canonical sources: `ChatMessage` (role, content, tool_calls, tool_call_id, name, created_at, content_format, truncatable_paths, token_count, plus the four reasoning fields after promotion) and `LLMRequest`.

| Canonical model | openai_compat (chat completions) | openai_responses (Responses API) | anthropic (Messages API) |
|---|---|---|---|
| system messages | kept in place as `role=system` messages (leading system messages sent verbatim) | merged into top-level `instructions` (multiple joined with a blank line; omitted when there are none) | concatenated into top-level `system` |
| user text content | `content: str`, or the part list for multimodal turns | user input item content | `text` block in the user message's content list |
| user image content (`ImageUrlPart`) | `image_url` content part | lowered to the Responses input image part | `image` block; data URL parsed into a base64 source, http(s) URL kept as the source URL form |
| assistant text | `content: str` (a part list folds to text) | message item with an `output_text` block | `text` block |
| assistant `tool_calls` | `tool_calls: [{id, type: "function", function: {name, arguments: <json str>}}]` | `function_call` item `{call_id, name, arguments: <str>}` | `tool_use` block `{id, name, input: <object>}` |
| TOOL message (tool result) | `{role: "tool", tool_call_id, content}` | `function_call_output` item `{call_id, output}` | `tool_result` block attached to the immediately following user turn (block precedes that turn's text when merged) |
| `tool_call_id` pairing | `tool_call_id` on the tool message | `call_id` pairs `function_call` with `function_call_output` | `tool_use_id` inside the `tool_result` block |
| `ChatMessage.name` | not lowered | not lowered | not lowered |
| `reasoning_content` | sent as assistant `reasoning_content`, only on tool-call turns (DeepSeek thinking rule) | carried inside the replayed reasoning item | `thinking` block, replayed on every assistant turn |
| `reasoning_signature` | not sent | not sent | `signature` field of the `thinking` block |
| `reasoning_item_id` | not sent | `item_reference` item id (store=true) | not sent |
| `reasoning_encrypted_content` | not sent | `encrypted_content` on the full reasoning item (store=false) plus top-level `include: ["reasoning.encrypted_content"]` | not sent |
| governance fields (`token_count`, `content_format`, `truncatable_paths`, `created_at`) | never on the wire (explicit construction) | never on the wire | never on the wire |
| `LLMRequest.model` | top-level `model` | top-level `model` | top-level `model` |
| `LLMRequest.messages` container | `messages` array | `input` item list | `messages` array, content as block lists |
| tools schema | nested `{type: "function", function: {name, description, parameters}}` | flat `{name, description, parameters}` | `{name, description, input_schema}` |
| `temperature` | `temperature` | `temperature` | `temperature` (clamped to 1.0) |
| `top_p` | `top_p` | `top_p` | `top_p` |
| `max_output_tokens` | `max_tokens` | `max_output_tokens` | `max_tokens`, required by the API (fallback 8192 when unset) |
| `reasoning_effort` | top-level `reasoning_effort` (NONE means omit) | `reasoning: {effort}` (NONE means omit) | `thinking: {type: "enabled", budget_tokens}` via the budget table (NONE means omit) |
| `stop` | `stop` | no wire field in V1 | `stop_sequences` |
| `prompt_cache_key` | top-level `prompt_cache_key` | top-level `prompt_cache_key` (the documented cache-routing hint) | no wire field — anthropic caches via `cache_control` breakpoints instead |
| `extra_body` | merged into body top level, user wins | same | same; the `thinking` key overrides the whole thinking object precisely |
| stream flags | `stream: true` plus `stream_options: {include_usage: true}` | `stream: true` plus `store` | `stream: true` |
| auth header | `Authorization: Bearer <key>` | `Authorization: Bearer <key>` | `x-api-key` plus `anthropic-version: 2023-06-01` |
| URL join | `{base}/chat/completions` | `{base}/responses` | base ends with `/v1` then `{base}/messages`, else `{base}/v1/messages` |

Notes on the three replay rules:

- **compat** replays `reasoning_content` only on assistant turns that carry tool calls. This is the DeepSeek thinking-mode rule inherited from the legacy `_sanitize_api_messages` behavior.
- **anthropic** replays a thinking block (content plus signature) on every assistant turn that has both fields, the opposite cadence from compat.
- **responses** replays reasoning via the full item when `store=false` (the default — flipped 2026-08-27, Errata below): the item carries `encrypted_content`, and the body adds `include: ["reasoning.encrypted_content"]`. With `store=true` (per-provider opt-in) the reasoning item id lowers to an `item_reference` item instead. A `store=false` replay whose item carries no `encrypted_content` is dropped with an ERROR log — the API rejects a bare replay item.

  Errata 2026-08-27: this bullet originally read "`store=true` (the default)". Third-party Responses endpoints (kimi and similar coding endpoints) reject `store=true` outright ("this service does not retain responses"), so the default flipped to `false` (ADR-0046 flip condition (c)); the no-encrypted-content drop guard was added with it (opencode filter semantics).

The `name` row: `ChatMessage.name` exists for OpenAI function-calling heritage but no engine lowers it. The tool name always travels inside each format's tool-call structure (nested `function.name` on chat, flat `name` on responses and anthropic).

The table above maps text content. Multimodal parts in TOOL messages (images from tool results) have per-format *placement* rules on top of the row mapping — native `tool_result` embed on anthropic, fold-text-plus-follow-up-user-message on compat/responses — specified in chapter 14.

## 4. Key Wire Facts per Protocol

### 4.1 openai_compat (chat completions)

Request:

- Data-only SSE frames: no `event:` line; the frame's event field is ignored.
- Body carries `stream: true` and `stream_options: {include_usage: true}`; without the latter the final usage frame never arrives.
- URL: `{base}/chat/completions` (trailing slash stripped from base).
- Auth: `Authorization: Bearer <key>`, empty dict when no key.

Stream:

- The `[DONE]` sentinel terminates the stream; on receipt the engine flushes all pending tool calls (when the recorded finish reason is LENGTH, pending accumulation is discarded), emits `UsageSnapshot`, then `Finish`.
- Usage arrives in a final chunk whose `choices` array is empty.
- `delta.tool_calls` entries key on `index` (the stream key). The `id` and `function.name` appear only in the first delta for a given index; subsequent deltas carry `index` plus an argument fragment, and fragments concatenate.
- A frame whose JSON fails to parse yields `StreamFailure` (MALFORMED) preserving already-emitted partial content.
- Think-tag extraction applies to `delta.content` only when `cfg.parse_think_tags` is true and only while the stream has produced no native `reasoning_content`.
- TOOL message content folds to text when it is a part list; empty content becomes `"(no output)"`.

### 4.2 openai_responses (Responses API)

Request:

- SSE frames are event+data pairs: the `event:` line names the event type, the `data:` line carries the JSON payload.
- Body carries `stream: true` and `store` (default false — see the Errata in chapter 3's replay notes); system messages become top-level `instructions`; tools use the flat schema; `reasoning: {effort}` appears only when the effort is not NONE; `max_output_tokens` passes through directly.
- `prompt_cache_key` passes through as the documented cache-routing hint — same-session requests land on the same cache node, which is what makes the automatic prefix cache observable as hit rate instead of scattering across nodes.
- URL: `{base}/responses` (`/v1` is already part of base).
- Auth: `Authorization: Bearer <key>`.

Stream:

- `item_id` is the tool stream key; `call_id` is the pairing key between `function_call` and `function_call_output` items. `ToolCallComplete` carries `call_id`, never `item_id`.
- `response.output_item.added` with type `function_call` starts a pending tool (key `item_id`, id `item.call_id`, name `item.name`).
- `response.function_call_arguments.delta` appends argument fragments.
- `response.output_item.done` (function_call) delivers the authoritative final arguments; the done value overrides accumulated deltas.
- `response.output_text.delta` maps to `TextDelta`. `response.reasoning_summary_text.delta` maps to `ReasoningDelta`; unknown delta-shaped reasoning events (for example `reasoning_text.delta`) map to `ReasoningDelta` as well.
- `response.completed` reconciles pending tools (they must be empty after the done events), emits `UsageSnapshot` from `response.usage`, then `Finish` with the stop mapping (incomplete to LENGTH) and the replay payload: this turn's reasoning item id, plus `encrypted_content` when the stream carried it.
- `response.failed` and `response.incomplete` yield `StreamFailure`.
- Unknown event types are skipped (debug-level log). Frames without an event line dispatch on `data.type` (gateway compatibility).

### 4.3 anthropic (Messages API)

Request:

- Headers `x-api-key` and `anthropic-version: 2023-06-01` are both required.
- System messages concatenate into the top-level `system` field.
- `max_tokens` is required by the API; the engine supplies `cfg.max_output_tokens`, falling back to 8192 when unset.
- Tools lower to `{name, description, input_schema}` with `input_schema` taken from the canonical parameters.
- Temperature clamps to 1.0 with an ERROR log when a value above 1.0 arrives (the API range is 0 to 1).
- Prompt caching is explicit opt-in on this protocol: without `cache_control` breakpoints the API caches nothing. The engine marks the system block and the final content block of each of the last two non-system messages with `{type: "ephemeral"}` (the opencode placement) — at most three breakpoints, under the API cap of four, by construction. The system field uses the content-block array form because a bare string carries no marker slot; prefix order is tools, system, messages, so the system breakpoint already covers the stable tools+system prefix.
- Consecutive same-role messages merge (adjacent user with user, adjacent assistant with assistant). This is a translation requirement of the Messages API, not a repair.
- TOOL messages lower to `tool_result` blocks attached to the immediately following user turn; when merged with that turn's text, `tool_result` blocks come first.
- Assistant turns replay a `thinking` block (content plus signature) on every turn where both fields are present. A thinking block without a signature is not replayed (ERROR log).
- The thinking budget comes from the effort mapping in chapter 9; `extra_body["thinking"]` overrides the whole object precisely.
- An `ImageUrlPart` with a data URL parses into `{type: "base64", media_type, data}`; an http(s) URL stays in the source URL form.

Stream:

- The content_block state machine keys on the block index: `content_block_start`, `content_block_delta`, `content_block_stop`.
- `text_delta` maps to `TextDelta`; `thinking_delta` maps to `ReasoningDelta`; `input_json_delta` appends tool argument fragments; `signature_delta` caches the signature.
- `content_block_stop` on a `tool_use` block finishes that tool and emits `ToolCallComplete`. On text and thinking blocks it is a no-op (the deltas already streamed).
- `message_start` records input usage; `message_delta` accumulates output usage and records the stop reason; `message_stop` flushes: finish all tool_use blocks, emit `UsageSnapshot`, then `Finish` carrying `ReplayFields(reasoning_content=<accumulated>, reasoning_signature=<cached signature>)`.
- Stop reason mapping: `end_turn` to STOP, `tool_use` to TOOL_CALLS, `max_tokens` to LENGTH, `refusal` to CONTENT_FILTER.
- `ping` frames are ignored; an `error` frame yields `StreamFailure`; unknown `data.type` values are skipped.

## 5. Degradation Policy: What Degrades, What Raises

Protocol engines validate structure and fail loud; they never silently repair history. But a long-running session must also survive one foreign block in deep history. The policy splits along one line.

| Situation | Policy | Why |
|---|---|---|
| Unrecognized content part in a message | ERROR log, skip the part | Skipping degrades one message; the remaining sequence stays legal |
| Role outside the four standard roles reaching an engine | ERROR log, merge to the nearest standard role | A path that bypasses pre-LLM assembly survives locally instead of sending an invalid role to a vendor |
| Orphan tool message (no matching assistant tool call) | Typed `ProtocolStructureError`; the provider converts it into an error `LLMResponse` | Skipping a pairing record corrupts the sequence |
| Dangling `tool_use` (no following tool result) | Typed `ProtocolStructureError` | Same |
| Unknown `chat_stream` kwarg | ERROR log, drop it | Unknown kwargs never enter the request body |
| Thinking block without a signature (anthropic replay) | Block not replayed, ERROR log | Keeps the request wire-legal |
| Unknown SSE event type / `data.type` | Skip, debug-level log | Forward compatibility |
| EOF without a terminal event | `StreamFailure` preserving partial content | Same philosophy as mid-stream errors: keep what arrived |

The dividing line, verbatim from ADR-0046: **"does skipping it leave the remaining message sequence legal"**. A skipped content part degrades one message. A skipped pairing record corrupts the sequence, so pairing violations raise instead.

Supporting rules:

- Internal-to-wire role normalization (`compact` to assistant, `system_reminder`/`agent` to user, via `normalize_agent_messages_for_llm`) stays in the pre-LLM context-assembly step where it lives today: protocol-agnostic, operating on the in-memory context copy. Engines accept only the four standard roles.
- `build_body` is explicit construction from the canonical model, never a filter over a serialized dict. Governance-internal fields (`token_count`, `content_format`, `truncatable_paths`, `created_at`) cannot appear on the wire by construction.
- The one surviving quirk parse inside an engine is think-tag extraction, because it parses provider output, which governance (a request-time concern) cannot reach.
- Content repair and history hygiene belong to the governance layer, a separate parallel workstream.

## 6. Transport Discipline

Five rules govern `HTTPStreamProvider`.

1. **Timeout is construction-only.** `httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)` with `max_retries=0`, set when the client is built and never per request. `read=None` is load-bearing: the httpx default 5-second read timeout kills long thinking streams.
2. **One idle mechanism.** `asyncio.wait_for(anext(frame_iterator), timeout=stream_idle_timeout)` is the only idle watchdog. On `TimeoutError`: `aclose` the iterator, yield `StreamFailure` (TIMEOUT), return. No second watchdog wraps the same concern.
3. **Terminal-event invariant.** A stream that ends without `Finish`/`StreamFailure` becomes an error response preserving partial content: `EventAssembler.result()` returns `finish_reason=ERROR` with `LLMErrorInfo(kind=TIMEOUT, should_retry=True)` and the accumulated content kept.
4. **Tool accumulation keys on the stream key.** Chat `index`, responses `item_id`, anthropic block `index`: never `call_id` (that is the pairing key). The generic `ToolStream` accumulator provides `start`, `append_or_start` (chat: identity arrives on the first delta only), `append_existing`, `finish`, `finish_with_input` (authoritative done-value wins over accumulated deltas), and `finish_all` (chat: no per-tool done signal). When the finish reason is LENGTH the caller discards pending accumulation; the rule is documented in the tool_stream module and enforced by the engines.
5. **Usage totals are computed.** `total_tokens` is the sum of the four disjoint counts, never read from the wire.

Transport error path: a non-2xx response is read (body truncated to 64KB), classified (`classify_http_error`: status plus body shape plus `Retry-After`), and yields a single `StreamFailure`. Connection failures become a CONNECTION error response rather than an exception, matching legacy provider behavior. Provider instances are constructed once per assembly on framework paths and cached per provider/model by `BotModelProvider` on the bot side, so per-turn model switching never constructs a provider plus HTTP client per turn.

## 7. Sampling Parameters: Defaults and Merge Priority

Merge priority: **call-site explicit argument > provider configuration > built-in default**. Model-level values from `model.yml` land in the provider configuration layer via `LLMConfig`, so they sit in the middle tier.

| Parameter | Built-in default | chat key | responses key | anthropic key |
|---|---|---|---|---|
| temperature | 0.7 | `temperature` | `temperature` | `temperature` (clamped to 1.0) |
| top_p | 0.95 | `top_p` | `top_p` | `top_p` |
| max_output_tokens | from model config (None) | `max_tokens` | `max_output_tokens` | `max_tokens` (required; fallback 8192) |
| reasoning_effort | NONE, meaning omit | `reasoning_effort` | `reasoning: {effort}` | `thinking: {type: "enabled", budget_tokens}` (budget table, chapter 9) |
| stop | None | `stop` | no wire field in V1 | `stop_sequences` |

Headers never carry sampling parameters; the `LLMRequest` envelope is the only carrier. Unknown kwargs reaching `chat_stream` are dropped with an ERROR log and never forwarded into the body. When the call site leaves the model unset, the provider's default model fills the (required) `LLMRequest.model`.

## 8. Configuration Chain: model.yml to HTTP Header

```
model.yml
  ProviderCfg: headers, endpoint_url, responses_store, api_key, base_url
  ModelCfg:    top_p (next to temperature / max_output_tokens / reasoning_effort)
      |
      v  synthesize_llm_config
LLMConfig (framework): headers, responses_store, endpoint_url, top_p, ...
      |
      v  create_llm_provider (InterfaceFormat routing, engine injection,
         URL resolution: endpoint_url verbatim, else engine url() join)
HTTPStreamProvider ctor: url (factory-resolved), headers, responses_store, top_p, ...
      |
      v
ProtocolConfig: api_key, max_output_tokens, reasoning_effort,
                extra_headers, store, parse_think_tags, extra_body
```

Facts along the chain:

- **Model names pass verbatim** (user ruling 2026-08-26): no prefix stripping, interface_format inference, or rejection anywhere in the call path — a stale `openai/`/`anthropic/` prefix reaches the API as part of the model name.
- **Header merge:** both sides are lowercased before merging, and the user wins. Overriding `Authorization` is a documented feature, not a bug. Lowercasing only one side would produce duplicate multi-value headers, and httpx would send both.
- **repr masking:** `LLMConfig.api_key` and `LLMConfig.headers` use `Field(repr=False)`. `model_dump()` is unaffected, so config persistence and WebUI round-trips keep working; an empty headers dict does not appear in the repr.
- **URL resolution (factory-side):** the factory resolves the request URL — `endpoint_url` used verbatim when non-empty (covers non-standard gateways), else the engine's per-format join on the normalized `base_url`: chat `{base}/chat/completions`; responses `{base}/responses`; anthropic base ending with `/v1` then `{base}/messages`, otherwise `{base}/v1/messages`. The provider requests the resolved URL unchanged.
- **api_key environment fallback:** the provider constructor falls back to `protocol.api_key_env` when `api_key` is empty: `OPENAI_API_KEY` for both OpenAI formats, `ANTHROPIC_API_KEY` for anthropic. This preserves the environment-variable semantics of the SDK-era providers.
- `GlobalModelConfig` mirrors `headers`/`responses_store`/`endpoint_url` and passes them through `to_llm_dict()`.
- WebUI: `ModelEditor` gains a headers editor (reusing `KeyValueEditor`), an optional `endpoint_url` input (placeholder notes that blank means auto-join by format), an `openai_response` option in the interface format dropdown, and a per-model `top_p` input. The backend schema ships before the UI, because `extra="forbid"` would reject the PUT otherwise.

## 9. Reasoning Replay Fields

`ChatMessage` is the canonical message model. Reasoning state rides on four declared fields, mirrored on `LLMResponse`:

| Field | On ChatMessage | On LLMResponse | Produced by | Consumed at lowering |
|---|---|---|---|---|
| `reasoning_content` | persisted; same key the legacy `model_extra` slot used, zero migration | event accumulation, or `Finish.replay` (replay wins) | `ReasoningDelta` accumulation; anthropic also delivers the final value via `Finish.replay` | compat: assistant tool-call turns only; anthropic: every assistant turn |
| `reasoning_signature` | persisted | `Finish.replay` | anthropic `signature_delta` cache | anthropic thinking block |
| `reasoning_item_id` | persisted | `Finish.replay` | responses reasoning item id | responses `item_reference` (store=true) |
| `reasoning_encrypted_content` | persisted | `Finish.replay` | responses stream, when carried | responses full reasoning item (store=false) plus the `include` list |

Transport channel: `Finish.replay` (`ReplayFields`) is the only replay channel. Engines expose zero per-response instance methods or attributes. The assembler copies replay fields into `LLMResponse`; the ReAct LLM node persists them onto the assistant message through the `build_assistant_message` seam (extended signature, default-None parameters, old callers unaffected).

Storage: the four fields land in the existing `message_json` residual column / JSONL with no schema change. `to_dict()` emits the same key names the `model_extra` era produced, so old rows read back into the declared fields automatically.

Anthropic thinking budget mapping (policy numbers, overridable):

| ReasoningEffort | budget_tokens |
|---|---|
| NONE | thinking not sent |
| MINIMAL | clamped to 1024 |
| LOW | 1024 |
| MEDIUM | 4096 |
| HIGH | 16384 |
| XHIGH | clamped to 16384 |
| MAX | clamped to 16384 |

`extra_body["thinking"]` precisely overrides the whole thinking object.

Two invariants from ADR-0046 worth restating: persisted history always keeps reasoning intact, and the send-or-not decision happens only at lowering time. Governance stays protocol-agnostic: it compresses and repairs a faithful context copy and never strips reasoning state.

## 10. Emitter Zero-Adaptation and the ReactLlmClient Event Loop

The emitter ABC (`emit_delta` / `emit_content` / `emit_stream_end` / `emit(event)`) is unchanged, so every existing emitter implementation (WebUI, QQ, Telegram) needs zero adaptation.

The proof is structural. The event loop is the only consumer of the stream, and it maps each event onto exactly the emit calls the legacy callback loop made, in the same order (delta, delta, ..., stream_end). Emitter timing-equivalence tests lock the sequence against the old implementation.

```python
async def call(...) -> LLMResponse:
    request = LLMRequest(
        model=provider.get_default_model(),
        messages=typed_messages,
        tools=...,
        temperature=ctx.temperature,
        max_output_tokens=ctx.max_output_tokens,
        prompt_cache_key=str(ctx.session),
    )
    events = provider.stream(request)
    if chain_has_llm_stream_scope:
        events = chain.around_llm_stream(ctx, stream_ctx, events)   # events in, events out
    assembler = EventAssembler(on_content_delta, on_reasoning_delta)
    try:
        async for ev in events:
            match ev:
                case TextDelta():
                    await drain_control_channel(); renew_deadline()
                    emitter.emit_delta(ev.text); emitter.emit(MODEL_OUTPUT, ev.text)
                    assembler.feed(ev)
                case ReasoningDelta():
                    await drain_control_channel(); renew_deadline()
                    emitter.emit(MODEL_REASONING, ev.text)
                    assembler.feed(ev)
                case _:
                    assembler.feed(ev)
        return assembler.result()
    except CancelledError:
        await events.aclose()
        persist_interrupted_partial(...)   # same logic as the legacy except block
```

Surrounding facts:

- The `wants_streaming` branch is preserved: non-streaming emitters drive the same stream path with emit calls suppressed at the dispatch point.
- Context-overflow recovery wrappers keep wrapping the new loop with their existing semantics.
- Interceptor event-ization: `around_llm_stream` takes a third parameter `AsyncIterator[LLMStreamEvent]` (type alias `LLMStreamEvents`) and returns the same type; the error path yields `StreamFailure` instead of raising through. `LlmCancelInterceptor` drains the control channel before each yield; its hard-cancel semantics are preserved (a `CANCEL_TURN` control message raises `AgentCancelledError`), and the exception propagates out of the loop into the except block, becoming INTERRUPTED_PARTIAL.
- The bridge on `CallbackStreamProvider`: the default `stream()` implementation runs `chat_stream` on a background task feeding an asyncio queue from the callbacks. The kwargs face replicates the current client call exactly and deliberately does not pass `model` (chapter 11). After `chat_stream` returns, the returned `LLMResponse` is re-translated into events: `TextDelta` for content that never went through callbacks (likewise `ReasoningDelta`), one `ToolCallComplete` per tool call (callbacks can never carry tool calls; without this re-translation the ReAct loop breaks on every bridged path), `UsageSnapshot` for non-default usage, then `Finish` (or `StreamFailure` when the response is an ERROR). An exception inside `chat_stream` ends the sequence with `StreamFailure` rather than propagating.
- `nodes/llm.py`: the strip_think fallback is deleted (the engines own think-tag parsing); `build_assistant_message` passes the replay fields through.

## 11. Existing-Code Migration Inventory

### 11.1 model_extra read sites (5)

Field promotion from `model_extra` to declared fields breaks nothing by itself, but every read site must move to attribute access. Missing the last two silently disables thinking in the legacy providers (the three legacy-provider files below were removed by the 2026-08-26 follow-up cleanup; they are recorded here as the V1 migration inventory):

- `src/modex_agent/trace/prompt_capture.py:336`
- `src/modex_agent/memory/pruned/render.py:97`
- `src/modex_agent/providers/openai_provider.py:419` (the conditional replay logic itself is unchanged; only the read moves)
- the legacy shared delta extractor, lines 24-36 (`extract_reasoning`; keeps a `model_extra` fallback branch because it also serves non-ChatMessage pydantic objects)
- `src/modex_agent/providers/litellm_provider.py:146-147` (`_get_attr_or_extra`)

### 11.2 usage read sites

- `src/modex_agent/hook/builtin/logging.py:70`
- `src/modex_agent/agents/summarizer/scoped_file_agent.py:61`
- `examples/bot_project/bot/eval/harbor/entry.py:213`
- `examples/bot_project/bot/eval/probes/budget.py:157` and `:199`
- `examples/bot_project/bot/eval/probes/_harness_execution.py:113`
- `agents/react/llm_client.py:202` (the `else {}` branch becomes `else TokenUsage()`)
- `trace/cassette.py:124` (`dict(resp.usage)` becomes `resp.usage.model_dump()`; the replay side normalizes old keys through the validator)
- `trace/chat_span_hook.py` usage reads become attribute access; the dual-key fallback logic is deleted because the validator already normalized; `turn_usage` accumulation and `UsageBuckets` key names follow
- all `LLMResponse(..., usage={...})` construction sites sweep to `TokenUsage` (dicts still validate through the before-validator; production code uses the explicit type)

(The usage fields on `CompactOutcome` consumers such as memory cleanup, dream engine, and memory trace hook are a different type and are not touched.)

### 11.3 eval direct instantiations (7)

All are environment-variable-driven constructions of the legacy litellm provider against openai-compatible endpoints, and all route through `create_llm_provider(LLMConfig(...))` instead:

- `examples/bot_project/bot/eval/cli.py:279` and `:916`
- `examples/bot_project/bot/eval/judge/runner.py:69`
- `examples/bot_project/bot/eval/probes/dispatch.py:64`
- `examples/bot_project/bot/eval/probes/generate.py:92`
- `examples/bot_project/bot/eval/sentinel/gate_cli.py:237`
- `examples/bot_project/bot/eval/live_gates/b1_cost_runtime.py:146`

### 11.4 Bridge kwargs-face replication

The cassette `llm_call_key` hashes model, temperature, max_output_tokens, tools, and kwargs. The current client never passes `model` to `chat_stream`, so the bridge must not pass it either; passing it would invalidate every existing cassette key. Any residual key drift is fixed in the bridge, never in the cassette key system. (ADR-0046's end state, content-addressed keys on the serialized `LLMRequest`, is a follow-up phase; see chapter 12.)

### 11.5 Other touch points

`multi_agent/factory.py`'s fallback provider goes through `create_llm_provider`; `GlobalModelConfig.to_llm_dict` passes the three new fields through; `media_utils.py` gets one docstring line updated (LiteLLM wording to protocol-engine wording); module `AGENTS.md` files are synced in a dedicated todo.

## 12. Out of Scope (Must-NOT-Have)

Transcribed from the work plan's hard constraints. Executors must not cross these lines.

1. **No deletion or cleanup of legacy provider parts in this phase.** The legacy SDK provider modules, the shared streaming-delta utilities, the streaming tool-call accumulator, and the interceptor chunk types stay as they are. Minimal adaptations to legacy files are limited to the exact lines the work plan whitelists (the usage typing in `core/types.py`, `cassette.py:124`, the chat_span_hook usage reads, `llm_client.py:202`, the openai provider's conditional-replay read, the shared delta extractor's `extract_reasoning`, `prompt_capture.py:336`, `pruned/render.py:97`, the chat_span_hook `turn_usage` remnants, the `media_utils.py` docstring line).
2. **ADR-0046 Decision 7 (deleting the legacy providers, litellm leaving the import graph) and the cassette re-record / key retirement are a follow-up plan**, gated on green evals. This phase is pure increment; the cassette key face is unchanged and the bridge replicates the legacy kwargs face. The deletion half was executed on 2026-08-26; the cassette re-record / key retirement remains future work.
3. **No emitter ABC change** (`emit_delta` / `emit_content` / `emit_stream_end` / `emit(event)`). WebUI, QQ, and Telegram emitter implementations need zero adaptation.
4. **No `ToolCallDelta` event variant** (comment reservation only). No Files API or document-file multimodal (`FilePart` is a slot comment only). No media types beyond images. *(V1 execution constraint — the V2 multimodal design, including `FilePart` and per-format tool-media placement, is specified in chapters 13–14; implementation is a follow-up plan.)*
5. **No cassette content-addressing redo** (only the minimal usage serialization adaptation). No `TODO(model-config-convergence)` topics.
6. **No `litellm` or `openai` SDK dependency in the new system**: `providers/http/` depends on `httpx` and the standard library only. No dsh-style adapter registry, replay envelope, or EMPTY_RESPONSE; only the EOF-without-terminal to `StreamFailure` slice is adopted.
7. **Zero silent repair inside protocol engines**: unrecognized content parts and roles degrade with an ERROR log (chapter 5); orphan tool messages and dangling `tool_use` raise typed errors; unknown kwargs are logged and dropped, never entering the request body.
8. **No governance changes**: `memory/context_governance.py` and its neighborhood belong to a parallel workstream.

## 13. Multimodal Input: Current-State Audit and Reference Implementations

*Supplemented 2026-08-26 after the provider rebuild landed. This chapter records what exists today (verified file-by-file), what the two reference implementations do, and the gaps the chapter-14 design closes. Design only — nothing here is implemented yet.*

### 13.1 What exists today (verified)

**Canonical layer — mostly right already:**

- `ContentPart = TextPart | ImageUrlPart`, discriminated by `type` (`core/message.py`). `ImageUrlPart{image_url: ImageUrl{url}}` where `url` is a `data:` URL (base64, media type embedded in the header) or an `http(s)://` URL. This shape is provider-neutral and maps 1:1 onto the OpenAI convention, so all three engines lower it cheaply. A `FilePart` slot comment is reserved at the union declaration.
- `ChatMessage.content: str | list[ContentPart] | None` — user-side multimodal content is expressible.
- `ToolResult.content: list[ContentPart]` is the source of truth (`core/tool_manager.py`); `message_content()` renders the LLM-facing text (joined `TextPart`s) and `to_message()` persists **text only** — the transient discipline already holds at the persistence boundary.
- `Modality` StrEnum (`core/capabilities.py`: `TEXT`/`IMAGE`/`VIDEO`/`AUDIO`, "adding a modality is one enum member", ADR-0013 §9) + `ModelCapabilities.supports()` + `ModelInfo` carried per turn.
- `Attachment` (`media/models.py`) — the bot-side upload record (id/kind/mime/path/locator), persisted as references in the transcript, never bytes (ADR-0013 §11).

**Ingestion paths — both exist, both work, both produce OpenAI wire dicts:**

- *User-provided*: adapter upload → `Attachment` → pipeline `turn_context_builder` sets `TurnCustomKey.INLINE_ATTACHMENTS` (turn state) → llm node `enrich_inline_media` Path 1 → `build_inline_image_block(att)` (compress per `attachments.image.{max_width,max_height,max_base64_bytes}` → data URL; caption + `image_url` dict pair, ADR-0014) → cached in `INLINE_IMAGE_CACHE` → injected **into the last user message's content**.
- *Tool-read*: the `read` tool gates on `Modality.IMAGE`, compresses, and returns `ToolResult(content=[TextPart, ImageUrlPart])` → the tool node stores `ToolMediaEntry{call_id, tool_name, image_blocks}` in `TurnCustomKey.TOOL_MEDIA_CACHE` → llm node Path 2 → `SyntheticUserMessageStrategy.inject_tool_media()` appends a **synthetic user message** (attribution text + image blocks) after the tool messages.

**Engine layer — user-message translation is per-format and correct:**

| engine | user-message image lowering |
|---|---|
| openai_compat | `_user_content` keeps `TextPart`/`ImageUrlPart` → `image_url` content parts |
| openai_responses | `ImageUrlPart` → `{type: "input_image", image_url}` |
| anthropic | `_part_blocks` → `image` block (data URL parsed into a base64 source; http URL kept as the url source) |

### 13.2 The gap

Tool-result media placement is **format-blind**. `SyntheticUserMessageStrategy` is the only implementation of the `ToolResultMediaStrategy` ABC and is hardcoded: every interface format gets the same synthetic user message, built from **OpenAI wire dicts at the dict layer** — format knowledge living above the engines, which violates the chapter-1 discipline ("wire knowledge lives only in engines").

Two concrete symptoms:

- The anthropic engine's **native** tool-result image path (`_tool_result_content` → `_part_blocks` → image blocks inside the `tool_result` block) exists and is tested at unit level, but is dead code in production: upstream never places parts in TOOL messages.
- The compat engine folds TOOL-message content text-only (`_fold_text` ERROR-skips an `ImageUrlPart`); if parts were placed in tool messages today, images would be dropped with ERROR logs on that path.

### 13.3 Reference implementation A — dsh (deepseek-harness)

- **Canonical**: `ImageBlock{attachment: ImageAttachmentRef}` where the ref is content-addressed (`sha256:…`, mediaType, bytes, dimensions, name) — durable messages and the session log carry **references only, never bytes, paths, or bearer URLs**. Request-time resolution produces a deterministic `RequestImageAttachment` per route policy (downscale/re-encode, cached by variant id).
- **Read tool**: separate `read_image` tool with a pre-flight capability gate (`assertImageCapableRoute` — refuses before any filesystem I/O when the route's `inputModalities` lacks image); persists the image via the attachment service *before* returning; renders `[text envelope, image block]`.
- **Placement (chat-completions serializer, hand-rolled)**: the tool message is string-only, so the text (including a per-image handle like `Image <attachmentId>; request image WxHpx.`) goes into the `role:"tool"` message, and the image parts are batched and flushed as **one following user message** labeled `Attached image(s) from tool result:`. Images prefer a Files-API `file_id` with inline base64 data-URL fallback; a stale `file_id` triggers invalidate → re-upload → retry-once.
- **Multi-provider path**: converts the canonical block into the neutral `{type:'image', data, mimeType}` and delegates per-provider wire shaping to the external pi-ai library (protocol table: openai-completions / openai-responses / anthropic-messages).
- **Degradations**: text-only routes get a stable text-placeholder projection (transient — durable history unchanged); over-budget images are offloaded oldest-first (also transient); a provider-rejected normalized image surfaces a diagnostic naming candidate causes.
- **Invariant**: "model-visible ⟺ logged" — session events carry the same blocks the model sees (refs), so replay is byte-faithful.

### 13.4 Reference implementation B — opencode

- **Canonical (native protocol stack)**: `ToolContent = ToolTextContent | ToolFileContent{mime, uri, name}`; user content carries media parts `{type:'media', data, mediaType}`. The `read` tool base64s image/PDF files and attaches `attachments: [{type:'file', mime, url: data URL}]` to the tool result; the session persists tool-part `state.attachments` (references + normalization, resized via `image.normalize`).
- **Per-protocol placement** (`packages/llm/src/protocols/`) — the exact shape chapter 14 adopts:
  - `openai-chat.ts`: tool messages lower to text only; image parts are collected into `pendingImages` and flushed as a **follow-up user message**.
  - `openai-responses.ts`: `function_call_output.output` accepts `string | content array`; tool-result media lowers natively as `[input_text, input_image]` arrays inside `function_call_output` — no flush.
    Errata 2026-08-27: the original audit note claimed `output` is a string and described the same collect-and-flush as `openai-chat.ts`. That premise was wrong — the official Responses API accepts `string | content array`, and opencode has since fixed this as a bug: tool-result media now embeds natively as `[input_text, input_image]` arrays inside `function_call_output.output`. Our audit snapshot had captured their older flush implementation.
  - `anthropic-messages.ts`: `tool_result` blocks carry image blocks **natively** (`{type:'image', source:{type:'base64', media_type, data}}`) — no flush.
  - `shared.ts`: `validateMedia` (allowed MIME set, encoded/decoded byte caps), `mimeToModality`.
- **Capability handling**: a per-provider `supportsMediaInToolResult` decision (anthropic/openai true, openai-compatible false) at the AI-SDK path; `unsupportedParts()` converts parts a model cannot consume into error text; compaction strips attachments on overflow.

### 13.5 Convergent lessons (both references, independently)

1. **Tool-result media placement is protocol knowledge.** The decision lives inside the per-format translation layer, never in a caller-side strategy.
2. **The follow-up user message is not a "strategy" — it is the wire constraint** of chat-completions and responses (their tool channels are string-only). Anthropic embeds natively. A caller-side "strategy" abstracts over a distinction the wire format already makes.
3. Both references **batch deferred tool images into one follow-up user message per contiguous tool run**, attributed to the originating calls.
4. Canonical parts are provider-neutral; wire shaping happens last, inside the engine.
5. Durable history never carries bytes — multimodal blocks are request-time projections (refs or transient merges).

## 14. Multimodal Input: Convergence Design (V2)

*Design only (2026-08-26). Implementation is a follow-up plan; this chapter is its spec. It extends — does not revise — the chapter-5 degradation policy and the chapter-1 layering discipline.*

### 14.1 Goals

- **G1 — one canonical representation, one injection point.** User-provided and tool-read media converge on `ContentPart`s inside `ChatMessage.content`; everything above the engines is format-blind.
- **G2 — placement is engine knowledge.** Each engine places media per its wire format's native affordance (anthropic embeds in `tool_result`; compat/responses fold text + flush a follow-up user message).
- **G3 — extensible modalities.** Adding a modality (documents first, audio/video later) follows a closed checklist; a half-finished addition degrades gracefully, never crashes.
- **G4 — persistence discipline unchanged.** Transient injection; persisted history stays text-only (mechanism B, ADR-0013 §10). Durable refs (dsh's model-visible⟺logged) are a future phase, explicitly out of scope.

### 14.2 Canonical model (V2 `ContentPart` union)

```
ContentPart = TextPart | ImageUrlPart | FilePart          # discriminated by `type`

ImageUrlPart { image_url: ImageUrl{url} }                  # unchanged; url = data: URL | http(s)://
FilePart     { file: FileUrl{url}, filename: str | None }  # V2 slot activates; same url convention
```

- `ImageUrlPart` keeps its shape: the data URL is self-describing (media type in the header), and it maps 1:1 onto all three wire conventions (`image_url` / `input_image` / anthropic source). No rename, no restructuring — the part is already right.
- `FilePart` mirrors the same url convention (a `data:application/pdf;base64,…` URL or an `http(s)://` link) plus `filename`. Media type parses from the data-URL header at lowering, exactly as the anthropic engine parses image sources today.
- **One mapping function is the modality authority**: `content_part_modality(part) -> Modality` (TextPart → TEXT, ImageUrlPart → IMAGE, FilePart → by media type — `application/pdf` and friends declare a `DOCUMENT` modality when that member is added). Capability gates and injection filtering consult this function only; no site re-derives modality from MIME strings ad hoc.

### 14.3 The two ingestion paths and their convergence point

Both paths end at the same place: **canonical parts merged into a per-call copy of a `ChatMessage`'s content.** The merge is transient; persisted history is untouched.

```
Path A — user-provided:
  adapter upload → Attachment (transcript persists references, ADR-0013 §11)
  → turn_context_builder: INLINE_ATTACHMENTS (turn state)                      [unchanged]
  → llm-node injection: render Attachment → [caption TextPart, ImageUrlPart]   [caption stays, ADR-0014]
      (compress per attachments.image.* config; cache by attachment id)
  → merge into the LAST USER message's content                                  [canonical parts, not wire dicts]

Path B — tool-read:
  read tool: capability gate → compress → ToolResult.content = [TextPart, ImageUrlPart]   [unchanged]
  → persistence: to_message() renders text only                                  [unchanged]
  → tool node: TOOL_MEDIA_CACHE[call_id] = ToolMediaEntry{call_id, tool_name,
      parts: list[ContentPart]}                                                  [parts now canonical, was wire dicts]
  → llm-node injection: merge entry.parts into the TOOL message with matching tool_call_id
```

**One injection function** (replaces `enrich_inline_media`'s two wire-dict paths):

```
inject_multimodal(messages: list[ChatMessage], turn_state) -> list[ChatMessage]
  1. gate: per-modality capability — merge only parts whose content_part_modality()
     the model's ModelCapabilities supports; unsupported parts drop with an ERROR log
     (the read tool's text hint already describes the file, so the model is not blind)
  2. Path A merge into the last user message
  3. Path B merge into tool messages by tool_call_id
  4. return a new list; history objects are never mutated
```

It runs on `ChatMessage`s **after governance** (governance sees text only — the existing ordering is load-bearing and unchanged) and before the engines. It is the single choke point where a future per-request budget/offload (dsh's `offloadRequestImagesWithPolicy`) would slot in — the seam is reserved by construction, not implemented.

Within-turn visibility follows today's semantics: the caches live in turn state, so every LLM call in the ReAct loop re-merges (a later step sees the image an earlier `read` produced); across turns the media is gone (mechanism B). *(Phase 1 shape. Phase 3's `inject_multimodal` v2 — §14.14 — keeps the same signature position and governance ordering but adds `media://` resolution + budget and deletes the turn-state caches: the history itself becomes the carrier.)*

### 14.4 Engine-side placement (the core of the design)

| engine | TOOL-message media | user-message media |
|---|---|---|
| anthropic | `_tool_result_content(msg.content)` → `_part_blocks` — parts lower to native `image`/`document` blocks **inside the `tool_result` block**. The code exists today; activation is upstream feeding parts. | `_user_blocks` → `_part_blocks` (today, unchanged) |
| openai_compat | tool message content folds to text (existing `_fold_text`; its ERROR-skip of media parts becomes the last-line guard); media parts are collected and **flushed as one follow-up user message** after the contiguous tool run: `[attribution TextParts, *media parts]` → `image_url` parts | `_user_content` keeps parts (today, unchanged) |
| openai_responses | tool-result media embeds **natively in function_call_output**: `output` = plain string for text-only results, otherwise a `[input_text, input_image]` content array riding its own `call_id` — no flush | `_user_content` → `input_image` (today, unchanged) |

Errata 2026-08-27: the openai_responses row originally read "`function_call_output.output` = folded text; media parts collected and flushed the same way → `input_image` / `input_file` parts". The string-only premise was wrong — the official Responses API accepts `string | content array` — so the responses engine now embeds tool-result media natively in function_call_output. The compat row's flush stands unchanged (the chat-completions tool channel is string-only — a protocol limit), and the flush rules below therefore apply to compat only.

Flush rules (compat engine only — its protocol-forced flush; the responses engine no longer flushes, see the errata above):

1. **Batch per contiguous tool run.** Collect deferred parts across consecutive TOOL messages; flush ONE user message immediately before the next non-tool message (or at the end of the list). Never one synthetic message per tool call.
2. **Attribution preserves per-call identity** — one text line per source call: `Media from tool '<tool_name>' (call <call_id>):` (keeps today's per-call attribution, which is deliberately richer than opencode's generic label).
3. The flush message is a request-time construction: it appears in the engine's lowering loop and in the cassette record's request view, never in persisted history.
4. Engines never consult capabilities to decide placement — the injection already gated. An engine receiving a part variant it cannot lower applies the chapter-5 policy (ERROR log, skip the part); a skipped image degrades one message, the sequence stays legal.

### 14.5 Capability gating and degradation

- **First line — read tool** (today, unchanged): refuses to produce image parts when the route lacks `Modality.IMAGE`; returns the objective text-only result.
- **Second line — injection gate**: per-modality filtering via `content_part_modality` + `ModelCapabilities` (replaces the IMAGE-only check; the gate generalizes as the union grows).
- **Last line — engine skip**: chapter-5's unrecognized-part policy extends to new part variants by construction (`case _:` in the part-lowering match). A half-implemented modality degrades with ERROR logs; it never corrupts a request.
- The compat engine's string-only tool channel is a **wire fact**, not a capability check — no `supports_media_in_tool_result`-style per-provider table is introduced. The format decides placement; the model's declared modalities decide whether parts are injected at all.
  Errata 2026-08-27: this bullet originally listed the responses engine's string-only `function_call_output` as a wire fact too. Only the compat (chat-completions) tool channel is string-only; the Responses API's `function_call_output.output` accepts `string | content array` (verified against the official API and the opencode/pi-ai reference implementations, which lower tool-result media as `[input_text, input_image]` arrays).

### 14.6 Persistence discipline (restated, unchanged)

- Persisted TOOL messages: text only (`to_message()` joins `TextPart`s).
- Persisted user messages: text plus mechanism-B path references.
- Canonical parts live in: the per-call message copy (transient), `TOOL_MEDIA_CACHE` / the attachment-render cache (turn state, transient).
- Cross-turn visibility is a Phase 3 concern with its own full design (§14.11–14.19): parts persist inside the message JSON as `media://` refs (opencode's carriage, zero SQL change) with bytes in the local media store, resolved to data URLs at request time.

### 14.7 Extensibility recipe — adding a modality

The closed checklist (documents → `FilePart` is the first application):

1. `Modality` enum member (one line, `core/capabilities.py`, ADR-0013 §9).
2. `ContentPart` variant in `core/message.py` (+ serialization round-trip in `to_dict`/coercion).
3. A row in `content_part_modality()` — the single authority.
4. `read` tool: extension routing + capability gate + compression policy section (`attachments.<kind>.*`).
5. Engine wire lowering, per format, as affordances allow:
   - anthropic: native block in `tool_result` and user content;
   - openai_responses: input part variants (`input_image` today, `input_file` for documents);
   - openai_compat: `image_url`-style part or, where the wire has no native shape (documents today), rely on the degradation path and record the gap here.
6. Bot adapters: upload-kind ingestion → `Attachment.kind`.
7. Tests: canned-wire tests per engine mirroring `tests/unit/providers/http/formats/` conventions + the read-tool gate test.

Steps 5–6 may land after 1–4; until they do, the new variant degrades via the chapter-5 skip. The recipe is deliberately ordered so the canonical layer can lead the wire layer.

### 14.8 Seams deleted (convergence rule 15)

- `ToolResultMediaStrategy` ABC and `SyntheticUserMessageStrategy` die — the decision they abstracted moves into the engines where it belongs.
- `ToolMediaEntry.image_blocks: list[dict]` (OpenAI wire dicts) becomes `parts: list[ContentPart]`.
- `build_inline_image_block`'s wire-dict return is replaced by a part-producing render (caption `TextPart` + `ImageUrlPart`); `ToolResult.content_blocks` (the OpenAI-dict computed field) is retired in favor of reading `content` parts directly.
- `enrich_inline_media` is renamed/replaced by `inject_multimodal` operating on `ChatMessage`s. No deprecation aliases — every caller migrates in the same change.

### 14.9 Test strategy

- **Canonical/injection** (rewrite of `test_tool_media_enrichment.py`): parts merged into the TOOL message by `call_id`; last-user-message merge for Path A; per-modality gate drops unsupported parts; empty-cache no-op; history objects unmutated.
- **Engine placement**: compat — folded tool text + exactly ONE follow-up user message with attribution lines and `image_url` parts (multi-step interleaving pinned); responses — `function_call_output.output` carries the tool result's media natively as a `[input_text, input_image]` array paired with its own `call_id`, with no follow-up user item (image-only results pin the array without an `input_text` element); anthropic — `tool_result` blocks carry native image blocks (the activation test for today's dead path).
  Errata 2026-08-27: the responses pin originally asserted "`function_call_output` text + flush with `input_image`" — updated to the native-array assertion above.
- **Regression**: `test_read_multimodal.py` unchanged (tool side untouched); engine format tests extend their canned-wire tables with one tool-media case each.
- **End-to-end**: one MockTransport scenario per format driving a read-image turn through `ReactLlmClient` — asserting the captured request body contains the media in that format's native placement.

### 14.10 Rollout phases

Authoritative phasing lives in §14.20 (with the Phase 3 lifecycle design of §14.11–14.19): ① canonical injection + engine placement (within-turn); ② `FilePart` + documents; ③ persisted ref parts — the full cross-turn lifecycle; ④ Files-API / mechanism A, separate spec.

### 14.11 Cross-turn persistence: storage decision (opencode evaluated)

*Full lifecycle design, 2026-08-26. Supersedes the earlier sketch. Design only.*

**What the references do (verified):**

- *dsh* persists to the session log as **content-addressed refs only** — the `tool/result` event carries the same image block the model saw, holding an `ImageAttachmentRef` (sha256 id, media type, dimensions, byte count; no base64, no path, no URL). Bytes live in the attachment store, re-resolved deterministically per request (per-route resize, variant-cached). Invariant: "model-visible ⟺ logged"; images survive turns and restarts; the log stays small.
- *opencode* persists **inline base64 data URLs** inside the session-store message JSON (tool-part `state.attachments`), re-sent on every subsequent request; compaction/overflow strips attachments because summaries cannot carry them. **No SQL schema is dedicated to media** — the parts ride the existing message JSON column.

**Feasibility of opencode's way on our side (verified against the code):**

- The schema move works unchanged: the message store already splits `content` into a typed column with an `is_content_json` companion (`ContentCodec`, ADR-0028's projection) — a `list[ContentPart]` persists and round-trips today, **zero SQL migration**. `ChatMessage.to_dict()` uses `model_dump(mode="json")`; `pruned/render._content_text` already renders part lists.
- The inline-base64 half is where we deviate. Four costs specific to our architecture: (1) the multi-layer memory (session → pruned → archive → core) would ingest base64 at every consolidation boundary — opencode strips at compaction for exactly this reason; (2) every `load_messages()` pulls all base64 into RAM for the whole window; (3) the cassette records `to_dict()` of request messages and would freeze megabytes of base64 into replay files; (4) we already run a byte store (`LocalFileMediaStore`, ADR-0013 §6) for user uploads — a second, inline one would be a divergent path.

**Decision: opencode's carriage, dsh's bytes.** Media travels inside the persisted message JSON (no SQL change, no side tables), but a part's url is a durable ref — `media://<attachment_id>` — resolved to a data URL at request time; bytes live in the media store on local disk. This is the opencode storage shape with our existing local-file substrate instead of inline base64, and it is the only variant that keeps every downstream text pipeline (memory, cassette, transcript) byte-free.

### 14.12 Canonical persisted form and round-trip

```
persisted TOOL message content:
  [ TextPart("[Image read: <path> (<mime>)]"),            # the text hint, as today
    ImageUrlPart(image_url=ImageUrl(url="media://<attachment_id>")) ]

persisted user message content (when the turn carried image attachments):
  [ TextPart(<sanitized user text + mechanism-B path-reference lines>),
    ImageUrlPart(image_url=ImageUrl(url="media://<attachment_id>")), ... ]
```

- `media://` is a pure framework convention inside `ImageUrl.url` (and later `FilePart.file.url`): scheme `media`, netloc = the media-store attachment id. No new part variant, no schema change — a ref is just a url whose resolution is deferred.
- Round-trip: `to_dict()`/`from_dict` carry the url string verbatim; `ContentCodec` marks the list as JSON content; `message_json` residual holds the rest. Backward compatibility is free — old rows (str content) and new rows (list content) already coexist behind `is_content_json`.
- The `attachment_id` is the existing `Attachment.id` (opaque, unique per stored file). Transcript records and the id→path index (ADR-0013 §11) keep working for user uploads; tool-read snapshots register in the same index with `locator=media`.

### 14.13 Ingestion paths, end-to-end

**Path A — user-provided (extends today's flow by one persisted step):**

```
adapter upload → gate (size/mime/AV) → LocalFileMediaStore.save(uploads/<sid>/<aid>)
→ Attachment record (transcript, id→path index)                [today, unchanged]
→ preprocess: sanitized text + mechanism-B path-reference lines [today, unchanged]
→ NEW: when ModelCapabilities supports IMAGE, the user ChatMessage is BUILT as a
  part list: [TextPart(sanitized text + path refs), ImageUrlPart(media://<aid>), …]
  and persisted that way (append_user_message path carries the list)
→ within-turn and cross-turn visibility come from the same persisted parts
```

- Text-only models keep today's behavior exactly: mechanism-B text references, no parts (the gate at message build).
- `INLINE_ATTACHMENTS` turn-state injection (enrich Path 1) is retired — the message itself is now the carrier.

**Path B — tool-read (dsh's persist-before-return):**

```
read tool: capability gate (Modality.IMAGE)                    [today, unchanged]
→ read bytes → compress (attachments.image.* policy)           [today, unchanged]
→ NEW: save the compressed snapshot to the media store BEFORE returning
      (reads/<sid>/<aid>; the snapshot is what was read — later edits to the
       workspace file do not rewrite history)
→ ToolResult.content = [TextPart(hint), ImageUrlPart(media://<aid>)]
→ build_tool_message keeps the part list on the ChatMessage
→ persisted as §14.12 (text hint + ref; never bytes)
```

- The snapshot discipline also fixes a silent hazard of deriving from the workspace file at request time: the file may change or vanish between the read and a later turn.

**Both paths converge before persistence**: a `ChatMessage` whose `content` is a part list carrying `media://` refs. There is exactly one producer per path (message build / tool result) and one resolver (below).

### 14.14 Request-time resolution: `inject_multimodal` (v2)

The §14.3 function gains a resolution step and the turn-state caches die — **the history is the carrier**:

```
inject_multimodal(messages: list[ChatMessage], caps, route_policy) -> list[ChatMessage]
  1. copy-on-write walk over messages
  2. for each message whose content is a part list:
       for each part with a media:// url:
         a. capability gate — part modality (content_part_modality) unsupported
            by caps → replace with nothing + ERROR log (the TextPart hint beside
            it already describes the file; the model is not blind)
         b. budget check — running encoded-byte count exceeds route budget →
            offload OLDEST-first to placeholders + ERROR log
         c. resolve media://<aid> → data URL via the compressor
            (per-route resize, variant cache keyed by aid+policy)
  3. return the new list; history objects never mutated
```

- **Engines never see a `media://` url** — after injection every part holds a data/http url, so chapter 14.4 placement applies unchanged.
- Within-turn visibility for free: the tool node appends the part-carrying message to history; the next step's LLM call re-walks the same history. `TOOL_MEDIA_CACHE`, `INLINE_IMAGE_CACHE`, and `INLINE_ATTACHMENTS` are all deleted — no side-channel state, no re-merge bookkeeping.
- The gate runs per request, so a mid-session model switch to a text-only model degrades old image parts gracefully (skipped + ERROR) instead of sending unsupported content.
- The budget is a request-time concern living exactly here (dsh's `offloadRequestImagesWithPolicy` discipline); the offload placeholder is a `TextPart` noting the offloaded media id.

### 14.15 Memory layers and consolidation

- **Session window**: parts ride messages; `load_messages()` re-materializes them. Refs are ~40 bytes — window RAM cost is negligible (this is the inline-model cost we avoided).
- **Compaction/pruning**: `pruned/render` extends `_content_text` — a media part renders as one bracket line (`[image: media://<aid>]`), mirroring the existing per-part rendering. Compacted summaries are text by construction; a pruned message's ref simply stops appearing in the LLM-facing window. **Compaction is the visibility horizon**: an image is model-visible exactly while its message survives uncompacted — which matches where its marginal value decays. No N-turn knob.
- **Archive/core consolidation**: never sees parts (works from compaction output, already text). Zero changes.
- **Orphaned bytes**: when compaction prunes a message, its media file becomes garbage — collected lazily at session teardown (§14.17), never at compaction time (no per-compaction IO).

### 14.16 Cassette, transcript, observability

- **Cassette**: `_record_llm` receives the post-injection message dicts (data URLs inline). The cassette sanitizes media parts when recording — data URL → `[media sha256=<digest>, <mime>, <n> bytes]` placeholder — keeping records small and diffable. Multimodal replay is explicitly out of cassette scope (it is a text-regression tool); a record whose request contained media replays with the placeholder and the provider's natural "unsupported/missing" behavior on the wire is acceptable, and recorded as such.
- **Transcript (bot side)**: user-message attachments already render via the Attachment records (ADR-0013 §11) — unchanged. Tool messages with parts flow through `message_delta` → persistence + ServerEvent; the transcript consumer ignores unknown part fields (text rendering uses the same bracket-line convention as §14.15), and a WebUI card for tool-read images can later key off the part ref — deliberately not required by this design.
- **Trace**: `prompt_capture` folds content with the shared `_content_text` semantics — media parts appear as bracket lines, never bytes.

### 14.17 Local-file lifecycle (media store)

- Layout today: `<media_dir>/uploads/<session_id>/<attachment_id>` (ADR-0013 §6, path-traversal-guarded). **Addition**: a sibling subtree `reads/<session_id>/<attachment_id>` for tool-read snapshots. The `LocalFileMediaStore` gains a save variant (or a `kind` parameter) and a `resolve(kind, session_id, attachment_id) -> bytes`; the uploads layout and API are untouched.
- **Write**: user upload at gate-accept (today) / tool read at snapshot time (§14.13).
- **Read**: only `inject_multimodal`'s resolver (variant-cached per route policy — one disk read per (aid, policy) per process).
- **Delete**: session teardown deletes both subtrees for that session (uploads already should; reads follow). An idempotent orphan sweep (files with no message referencing them) runs at teardown after deletion of the session's rows — cheap directory walk, no refcount machinery.
- **Workspace files are NOT the store**: the agent's own files stay where they are; only the compressed snapshot is persisted under `reads/`. A vanished workspace file therefore cannot corrupt what the model already read.

### 14.18 Degradation matrix

| Situation | Behavior | Layer |
|---|---|---|
| Route lacks the part's modality | part skipped at injection, ERROR log; sibling text hint remains | injection gate |
| Request budget exceeded | oldest parts offloaded to placeholder TextParts, ERROR log | injection budget |
| `media://` file missing (store swept early, disk loss) | placeholder part + ERROR log; never a crash, never bytes-on-wire | resolver |
| Media file corrupted (decode fails) | same as missing | resolver |
| Compaction prunes the message | ref renders as bracket line; visibility ends naturally | memory |
| Engine cannot lower a part variant (future modality, engine not yet extended) | chapter-5 policy: ERROR log, skip the part | engine |
| Text-only model at ingest | no parts produced at all (mechanism B) | message build / read tool |

### 14.19 Cleanup inventory: the hardcoded seams this design retires

Deleted outright (no aliases — convergence rule 15):

- `media/tool_media.py`: `ToolResultMediaStrategy` ABC, `SyntheticUserMessageStrategy`, `ToolMediaEntry` — the strategy seam moves into the engines (§14.4), the entry into the persisted message.
- `agents/react/nodes/llm.py`: `enrich_inline_media`, `_inject_into_last_user_message`, `_default_tool_media_strategy` — replaced by `inject_multimodal` (a new module sibling, e.g. `agents/react/media_injection.py`, keeping the llm node thin).
- `TurnCustomKey.INLINE_ATTACHMENTS` / `INLINE_IMAGE_CACHE` / `TOOL_MEDIA_CACHE` — the history is the carrier.
- The tool node's `TOOL_MEDIA_CACHE` collection block (`nodes/tool.py`).
- `media_utils.build_inline_image_block` (wire-dict producer) — replaced by a part-producing render `render_attachment_parts(att) -> list[ContentPart]` (caption TextPart + `ImageUrlPart(media://…)`); the compressor core it wraps is reused by the resolver.
- `ToolResult.content_blocks` computed field (OpenAI wire dicts) — consumers read `content` parts; `image_blocks` stays as the typed-part view only if a consumer remains, else retires too.

Rewritten: `read` tool (snapshot save + ref part), `turn_context_builder.preprocess` (user message part list), `build_tool_message` (keep parts), `pruned/render._content_text` (bracket-line for media parts), `trace/prompt_capture._content_to_text` (parts render as bracket lines / `{"type":"image","url":"media://…"}` gen_ai parts — replacing the JSON-serialize-and-truncate path so span attributes and the Langfuse prompt view carry refs, never bytes or truncated base64), `tool_media` tests → `media_injection` tests, `test_inline_image_block.py` → part-render tests, `test_tool_media_enrichment.py` → injection v2 tests (incl. cross-turn: history reload → resolution → engine placement).

### 14.20 Rollout phases (updated)

1. **Phase 1**: canonical injection + engine placement + seam deletion of §14.8 (within-turn only; persistence stays text-only).
2. **Phase 2**: `FilePart` + document modality via the §14.7 recipe.
3. **Phase 3 (this section's design)**: persisted ref parts — media-store `reads/` subtree, snapshot-before-return, user-message part lists, injection v2 with resolver + budget, cassette sanitize, teardown GC. Landed as one plan; §14.19's deletions ride with it (several are created new by Phase 1 and immediately superseded — acceptable, Phase 1's shape is what makes Phase 3 small).
4. **Phase 4 (separate spec)**: Files-API upload with `file_id` (mechanism A, ADR-0013 §10) and per-modality route policies richer than the global budget.

> Status: **Phase 1 + Phase 3 IMPLEMENTED 2026-08-26** (plan `.omo/plans/multimodal-input-lifecycle.md`, commits 636056ee..63ede610 — §14.19 seam deletions verified zero-residue); Phase 2/4 remain future work.
