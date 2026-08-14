# OpenTelemetry GenAI Semantic Conventions — Reference

> Source: [open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)
> Stability: `development` (all attributes subject to change)

## Span Types Emitted

ModexAgent emits 8 span types, assembled by `build_trace_hooks()` based
on the configured `TraceSpanMode` tier (see [Tier Configuration](#tier-configuration)).
Each specialized hook class independently emits its own span — no central
collection at turn end.

### 1. Agent turn (`invoke_agent` span)

Span kind: `INTERNAL`. Root span — emitted once at `finally_graph` by
`RootSpanHook`. The span is pre-registered at `start_node_turn` (span_id +
start_time stored in `TraceSessionState`, trigger message captured) but not
written until `finally_graph`, which emits the complete span with input +
output + `end_time` + aggregated `gen_ai.usage.*` + `stop_reason` in a
single write. This avoids the Langfuse v4 immutability issue where
double-emission (start at `before_turn` + complete at `finally_turn`)
produced two separate observations instead of one merged span.

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

### 2. Agent start (`agent.start` span)

Span kind: `INTERNAL`. Emitted at `start_node_turn` by `AgentStartSpanHook`
(registered after `RootSpanHook`, so the root span ID is available). Fresh
turn only. Carries the system prompt and tool definitions. `FULL` tier only.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.system_instructions` | string | emitted (full system prompt, when `prompt_capture != off`) |
| `gen_ai.system.prompt_hash` | string | emitted (SHA-256 first 16 chars, when `prompt_capture != off`) |
| `gen_ai.system.prompt_length` | int | emitted (when `prompt_capture != off`) |
| `gen_ai.tool.definitions` | list | emitted (full tool definitions, when `capture_tools = True`) |
| `gen_ai.agent.name` | string | emitted |
| `gen_ai.operation.name` | enum | emitted |
| `langfuse.observation.type` | `span` | emitted |

### 3. LLM call (`chat` span)

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

### 4. Tool batch (`execute_tool_batch` span)

Span kind: `INTERNAL`. Emitted by `ToolSpanHook` at `after_tool_execution`.
One per tool batch — groups the individual `execute_tool` child spans that
follow. The batch span ID is cached at `before_tool_execution` so each
per-tool span parents to it.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.operation.name` | enum (`execute_tool`) | emitted |
| `gen_ai.tool.count` | int | emitted (custom) |
| `gen_ai.tool.names` | string[] | emitted (custom) |
| `gen_ai.agent.name` | string | emitted |
| `langfuse.observation.type` | `span` | emitted |

### 5. Tool execution (`execute_tool` span)

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

### 6. Iteration (`iteration` span)

Span kind: `INTERNAL`. One per ReAct iteration — `before_iteration` caches
start time, `after_iteration` emits the complete span.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.iteration.number` | int | emitted (custom) |
| `langfuse.observation.type` | `span` | emitted |
| `langfuse.observation.input` | JSON string | emitted (`{iteration: N}`) |
| `langfuse.observation.output` | JSON string | emitted (`{iteration: N, duration_ms: ...}`) |

### 7. Human review (`human_review` span)

Span kind: `INTERNAL`. Emitted at `after_approval`.

| Attribute | Type | Status |
|-----------|------|--------|
| `langfuse.observation.type` | `event` | emitted |
| `langfuse.observation.level` | enum | emitted (`WARNING` on denial, `DEFAULT` otherwise) |

### 8. Multi-agent handoff (`agent.handoff` span)

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

All data collection is through 7 specialized hook classes, assembled by
`build_trace_hooks()` based on the `TraceSpanMode` tier (see
[Tier Configuration](#tier-configuration)). No hardcoded trace emission in
business code. One `TraceSessionState` is shared across every hook in a
single factory call so child spans can resolve the root span ID seeded by
`RootSpanHook`.

| Hook class | Hook ABC(s) | Method(s) | Span(s) emitted | obs type |
|---|---|---|---|---|
| `RootSpanHook` | `StartNodeTurnHook`, `FinallyGraphHook` | `start_node_turn`, `finally_graph` | `invoke_agent` (once, at `finally_graph`) | agent |
| `AgentStartSpanHook` | `StartNodeTurnHook` | `start_node_turn` | `agent.start` | span |
| `ChatSpanHook` | `BeforeLLMHook`, `AfterLLMResponseHook` | `before_llm`, `after_llm_response` | `chat` | generation |
| `ToolSpanHook` | `BeforeToolExecutionHook`, `AfterToolExecutionHook` | `before_tool_execution`, `after_tool_execution` | `execute_tool_batch` + `execute_tool` | span / tool |
| `HandoffSpanHook` | `AfterToolExecutionHook` | `after_tool_execution` | `agent.handoff`* | span |
| `ApprovalSpanHook` | `AfterApprovalHook` | `after_approval` | `human.review` | event |
| `IterationSpanHook` | `BeforeIterationHook`, `AfterIterationHook` | `before_iteration`, `after_iteration` | `iteration.start` + `iteration.end` | span |

\* `agent.handoff` emitted only when `tool_name` is `send_to_agent` or `task`
— detects multi-agent control transfer via the tool execution hook.

Registration order is execution order (`HookRunner` dispatches in
registration order). `RootSpanHook` is always first (seeds the trace/root
span IDs). `ToolSpanHook` precedes `HandoffSpanHook` so the batch span the
handoff parents to exists by the time the handoff hook reads it. Each hook
is wrapped in a `HookSpec` with `HookErrorPolicy.LOG` so a failing trace
hook logs and continues rather than crashing the agent.

## Tier Configuration

`TraceSpanMode` controls which hooks are registered, and therefore which
spans are emitted. The factory (`build_trace_hooks()`) selects hooks based
on the `trace_spans` config field.

| Tier | Hooks | Spans emitted |
|---|---|---|
| `minimal` | `RootSpanHook` (1) | `invoke_agent` only |
| `standard` (default) | root + chat + tool + handoff + approval (5) | `invoke_agent`, `chat`, `execute_tool_batch`, `execute_tool`, `agent.handoff`, `human.review` |
| `full` | all 7 hooks | standard spans + `agent.start` + `iteration.start` + `iteration.end` |

Two additional config fields control prompt and tool capture scope:

- `prompt_capture: PromptCaptureMode` — `off` (no prompt content), `hash`
  (system prompt hash + length only), `summary` (default: hash + length +
  last N messages truncated), `full` (full system prompt + tools + all
  messages untruncated). Controls the `chat` span's input message capture
  and the `agent.start` span's system prompt storage (`off` disables both).
- `capture_tools: bool` — when `True`, `gen_ai.tool.definitions` is
  included on the `agent.start` span. Default `False`. Only effective at
  `full` tier (the `agent.start` span is not emitted at lower tiers).

## Backend Modes (trace_backend config)

| Mode | Local JSONL | OTLP HTTP POST | Use case |
|---|---|---|---|
| `off` | No | No | Zero overhead, no tracing |
| `file` (default) | Yes | No | Local debugging, training data export |
| `otel_http` | Yes | Yes | Langfuse / Phoenix / Datadog remote export |

`build_trace_stores()` factory: `off` returns `None` (hook's `_save_span`
no-ops when `trace_store is None`). `file` and `otel_http` both write local
JSONL; `otel_http` additionally POSTs OTLP JSON. JSON OTLP path is decoupled
from the OTel SDK tracer — only needs `httpx.Client`, not the SDK.

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

## Audit Status

Verified against:
- Langfuse v3.224.1 source (commit `5e4dae6e`) — OtelIngestionProcessor.ts, attributes.ts, ObservationTypeMapper.ts
- OTel GenAI semantic conventions (open-telemetry/semantic-conventions-genai)

All Langfuse-mappable fields are correctly set. OTel GenAI Required + Recommended
attributes fully covered. The `otel_http` backend decoupled from SDK tracer —
JSON OTLP works independently via `httpx.Client`.
