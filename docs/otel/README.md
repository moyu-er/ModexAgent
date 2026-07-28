# OpenTelemetry GenAI Semantic Conventions — Reference

> Source: [open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)
> Stability: `development` (all attributes subject to change)

## Span Types Emitted

ModexAgent emits 6 span types. Each hook independently emits its own span —
no central collection at turn end.

### 1. Agent turn (`invoke_agent` span)

Span kind: `INTERNAL`. Root span — emitted twice with the same `span_id`:
- `before_turn`: start span with `langfuse.observation.input` (trigger message)
- `finally_turn`: complete span with `langfuse.observation.output` (final reply),
  `end_time`, aggregated `gen_ai.usage.*`

Langfuse merges both emissions by `span_id`.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.operation.name` | enum (`invoke_agent`) | emitted |
| `gen_ai.agent.name` | string | emitted |
| `gen_ai.conversation.id` | string | emitted |
| `gen_ai.provider.name` | string | emitted |
| `gen_ai.response.finish_reasons` | string[] | emitted |
| `gen_ai.usage.input_tokens` | int | emitted (aggregated across LLM calls) |
| `gen_ai.usage.output_tokens` | int | emitted (aggregated) |
| `gen_ai.usage.cache_read.input_tokens` | int | emitted (aggregated) |
| `gen_ai.usage.cache_creation.input_tokens` | int | emitted (aggregated) |
| `gen_ai.usage.reasoning.output_tokens` | int | emitted (aggregated) |
| `gen_ai.output.messages` | list (parts-based) | emitted |
| `langfuse.observation.type` | `agent` | emitted |
| `langfuse.observation.input` | JSON string | emitted (trigger message) |
| `langfuse.observation.output` | JSON string | emitted (final assistant reply) |
| `langfuse.trace.input` | JSON string | emitted (same as obs.input) |
| `langfuse.trace.output` | JSON string | emitted (same as obs.output) |
| `langfuse.trace.name` | string | emitted (`{session_id}.{turn_id}`) |
| `langfuse.internal.as_root` | bool | emitted (`true`) |

### 2. LLM call (`chat` span)

Span kind: `CLIENT`. Emitted at `after_llm_response`.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.operation.name` | enum (`chat`) | emitted |
| `gen_ai.request.model` | string | emitted |
| `gen_ai.response.model` | string | emitted |
| `gen_ai.response.finish_reasons` | string[] | emitted |
| `gen_ai.usage.input_tokens` | int | emitted |
| `gen_ai.usage.output_tokens` | int | emitted |
| `gen_ai.usage.cache_read.input_tokens` | int | emitted |
| `gen_ai.usage.cache_creation.input_tokens` | int | emitted |
| `gen_ai.usage.reasoning.output_tokens` | int | emitted |
| `gen_ai.input.messages` | list (parts-based) | emitted (via PromptCaptureStrategy) |
| `gen_ai.output.messages` | list (parts-based) | emitted |
| `gen_ai.prompt` | JSON string | emitted (Langfuse compat) |
| `gen_ai.completion` | string | emitted (Langfuse compat) |
| `gen_ai.request.temperature` | float | emitted |
| `gen_ai.request.max_tokens` | int | emitted |
| `gen_ai.request.stream` | bool | emitted |
| `gen_ai.api.duration_s` | float | emitted (custom) |
| `langfuse.observation.type` | `generation` | emitted |
| `langfuse.observation.input` | JSON string | emitted (captured messages) |
| `langfuse.observation.output` | JSON string | emitted (LLM response, includes tool_calls) |

### 3. Tool execution (`execute_tool` span)

Span kind: `INTERNAL`. One per tool result.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.operation.name` | enum (`execute_tool`) | emitted |
| `gen_ai.tool.name` | string | emitted |
| `gen_ai.tool.type` | string (`function`) | emitted |
| `gen_ai.tool.call.id` | string | emitted (when available) |
| `gen_ai.tool.call.result` | string | emitted |
| `gen_ai.tool.success` | bool | emitted (custom) |
| `gen_ai.tool.fail` | bool | emitted (custom) |
| `error.type` | string | emitted (on errors) |
| `langfuse.observation.type` | `tool` | emitted |
| `langfuse.observation.input` | JSON string | emitted (`{tool_name: ...}`) |
| `langfuse.observation.output` | JSON string | emitted (`{result: ...}`) |

### 4. Iteration (`iteration` span)

Span kind: `INTERNAL`. One per ReAct iteration — `before_iteration` caches
start time, `after_iteration` emits the complete span.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.iteration.number` | int | emitted (custom) |
| `langfuse.observation.type` | `span` | emitted |
| `langfuse.observation.input` | JSON string | emitted (`{iteration: N}`) |
| `langfuse.observation.output` | JSON string | emitted (`{iteration: N, duration_ms: ...}`) |

### 5. Human review (`human_review` span)

Span kind: `INTERNAL`. Emitted at `after_approval`.

| Attribute | Type | Status |
|-----------|------|--------|
| `langfuse.observation.type` | `event` | emitted |
| `langfuse.observation.level` | enum | emitted (`WARNING` on denial, `DEFAULT` otherwise) |

### 6. Multi-agent handoff (`agent.handoff` span)

Span kind: `INTERNAL`. Emitted at `send_to_agent` dispatch point.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.operation.name` | enum (`invoke_agent`) | emitted |
| `gen_ai.agent.name` | string | emitted |
| `langfuse.session.id` | string | emitted |
| `langfuse.user.id` | string | emitted |
| `langfuse.observation.type` | `span` | emitted |

## Trace-Level Attributes (on all spans)

These are set on every span so Langfuse can populate trace fields regardless
of which span arrives first:

| Attribute | Langfuse field |
|---|---|
| `langfuse.session.id` | trace `sessionId` |
| `langfuse.user.id` | trace `userId` |
| `langfuse.trace.name` | trace `name` (`{session_id}.{turn_id}`) |

## Export Architecture

Direct JSON OTLP HTTP POST (bypasses OTel SDK) — preserves our `trace_id` /
`span_id` / `parent_span_id` for correct parent-child trace tree. The OTel
SDK's `start_span` generates its own `trace_id`, breaking relationships.

`OtelSpanTraceStore.save_span()` writes local `spans.jsonl` first, then POSTs
OTLP JSON if `otel_endpoint` is configured. Failures are logged but do not
block the bot.

## Hook → Span Mapping

All data collection is through hook ABCs — no hardcoded trace emission in
business code.

| Hook ABC | Method | Span emitted | obs type |
|---|---|---|---|
| `BeforeTurnHook` | `before_turn` | `invoke_agent` (initial: input + as_root) | agent |
| `BeforeLLMHook` | `before_llm` | — (caches request) | — |
| `AfterLLMResponseHook` | `after_llm_response` | `chat` | generation |
| `BeforeToolExecutionHook` | `before_tool_execution` | — (caches tool_calls) | — |
| `AfterToolExecutionHook` | `after_tool_execution` | `execute_tool` + `agent.handoff`* | tool / span |
| `BeforeIterationHook` | `before_iteration` | `iteration.start` | span |
| `AfterIterationHook` | `after_iteration` | `iteration.end` | span |
| `AfterApprovalHook` | `after_approval` | `human.review` | event |
| `FinallyTurnHook` | `finally_turn` | `invoke_agent` (completed: output + usage) | agent |

\* `agent.handoff` emitted only when `tool_name == "send_to_agent"` — detects
multi-agent control transfer via the tool execution hook, replacing the
previous hardcoded `_emit_handoff_span` in `AgentCommunicationService`.

## Backend Modes (trace_backend config)

| Mode | Local JSONL | OTLP HTTP POST | Use case |
|---|---|---|---|
| `off` | No | No | Zero overhead, no tracing |
| `file` (default) | Yes | No | Local debugging, training data export |
| `otel_http` | Yes | Yes | Langfuse / Phoenix / Datadog remote export |

`build_trace_stores()` factory: `off` returns `None` (hook's `_save_span`
no-ops when `trace_store is None`). `file` and `otel_http` both write local
JSONL; `otel_http` additionally POSTs OTLP JSON. JSON OTLP path is decoupled
from the OTel SDK tracer — only needs `requests.Session`, not the SDK.

## Message Format

Messages use the OTel parts-based format. Roles preserved as-is (`user`,
`agent`, `assistant`, `tool`) — no role conversion.

```json
[
  {"role": "user", "parts": [{"type": "text", "content": "Read config.py"}]},
  {"role": "assistant", "parts": [{"type": "tool_call", "name": "read_file", "arguments": "{\"path\":\"config.py\"}"}]},
  {"role": "tool", "parts": [{"type": "text", "content": "DEBUG = True"}]},
  {"role": "assistant", "parts": [{"type": "text", "content": "Config sets DEBUG=True."}]}
]
```

Content is not truncated — full message text is stored.

## `gen_ai.operation.name` Enum Values

`chat`, `generate_content`, `text_completion`, `embeddings`, `retrieval`,
`create_agent`, `invoke_agent`, `execute_tool`, `invoke_workflow`, `plan`,
`search_memory`, `create_memory`, `update_memory`, `upsert_memory`,
`delete_memory`, `create_memory_store`, `delete_memory_store`

## `gen_ai.provider.name` Enum Values

`openai`, `gcp.gen_ai`, `gcp.vertex_ai`, `gcp.gemini`, `anthropic`, `cohere`,
`azure.ai.inference`, `azure.ai.openai`, `ibm.watsonx.ai`, `aws.bedrock`,
`perplexity`, `x_ai`, `deepseek`, `groq`, `mistral_ai`, `moonshot_ai`

## Known Limitations (data-source unavailable, not implementation gaps)

| Attribute | Reason |
|---|---|
| `gen_ai.response.id` | `LLMResponse` has no `id` field (provider doesn't surface response ID) |
| `gen_ai.tool.call.arguments` (per-tool span) | `ToolResult` carries no `arguments` (args live on `ToolCall`, captured at batch level) |
| `gen_ai.request.top_p` / `frequency_penalty` / `presence_penalty` | `LLMConfig` has no corresponding fields |
| `gen_ai.system_instructions` (raw text) | Only hash + length emitted (PII protection); raw text is opt-in and not enabled |

## Audit Status

Verified against:
- Langfuse v3.224.1 source (commit `5e4dae6e`) — OtelIngestionProcessor.ts, attributes.ts, ObservationTypeMapper.ts
- OTel GenAI semantic conventions (open-telemetry/semantic-conventions-genai)

All Langfuse-mappable fields are correctly set. OTel GenAI Required + Recommended
attributes fully covered. The `otel_http` backend decoupled from SDK tracer —
JSON OTLP works independently via `requests.Session`.
