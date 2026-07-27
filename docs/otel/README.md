# OpenTelemetry GenAI Semantic Conventions — Reference

> Source: [open-telemetry/semantic-conventions-genai](https://github.com/open-telemetry/semantic-conventions-genai)
> Stability: `development` (all attributes subject to change)

## Span Types Emitted

ModexAgent emits 5 span types. Each uses the standard attributes below.

### 1. LLM call (`chat` span)

Span kind: `CLIENT`. Maps to OTel `gen_ai.inference.client`.

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
| `gen_ai.input.messages` | any (JSON, parts-based) | emitted (opt-in via PromptCaptureStrategy) |
| `gen_ai.output.messages` | any (JSON, parts-based) | emitted |
| `gen_ai.conversation.id` | string | emitted |
| `gen_ai.agent.name` | string | emitted |
| `gen_ai.api.duration_s` | float | emitted (custom — LLM wall-clock duration) |

Not emitted (not available in framework): `gen_ai.provider.name`, `gen_ai.response.id`.

### 2. Agent turn (`invoke_agent` span)

Span kind: `INTERNAL`. Maps to OTel `gen_ai.invoke_agent.internal`.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.operation.name` | enum (`invoke_agent`) | emitted |
| `gen_ai.agent.name` | string | emitted |
| `gen_ai.conversation.id` | string | emitted |
| `gen_ai.response.finish_reasons` | string[] | emitted |

### 3. Tool execution (`execute_tool` span)

Span kind: `INTERNAL`. Maps to OTel `gen_ai.execute_tool.internal`.

| Attribute | Type | Status |
|-----------|------|--------|
| `gen_ai.operation.name` | enum (`execute_tool`) | emitted |
| `gen_ai.tool.name` | string | emitted |
| `gen_ai.tool.type` | string (`function`) | emitted |
| `gen_ai.tool.call.id` | string | emitted (when available) |
| `gen_ai.tool.call.result` | any (JSON) | emitted (opt-in) |
| `error.type` | string | emitted (on errors) |
| `gen_ai.tool.success` | bool | emitted (custom) |
| `gen_ai.tool.fail` | bool | emitted (custom) |

### 4. Iteration boundary (`iteration.start` / `iteration.end`)

Custom spans (not in OTel standard). Emitted for per-iteration grouping.

### 5. Multi-agent handoff (`agent.handoff`)

Custom span (not in OTel standard). Emitted at `send_to_agent` dispatch point.

## Langfuse Compatibility Layer

Langfuse maps `input`/`output` from legacy names, not current OTel standard.
Both are emitted for dual compatibility:

| OTel standard | Langfuse compat | Langfuse field |
|---|---|---|
| `gen_ai.input.messages` (parts-based) | `gen_ai.prompt` (JSON string) | observation `input` |
| `gen_ai.output.messages` (parts-based) | `gen_ai.completion` (string) | observation `output` |
| — | `langfuse.session.id` | trace `sessionId` |
| — | `langfuse.user.id` | trace `userId` |
| — | `langfuse.observation.type` (`generation`) | observation `type` |

See `docs/langfuse/README.md` for the full Langfuse OTLP attribute mapping.

## Message Format

Messages use the OTel parts-based format (NOT simple `{role, content}`):

```json
[
  {"role": "user", "parts": [{"type": "text", "content": "What is 2+2?"}]},
  {"role": "assistant", "parts": [{"type": "tool_call", "id": "call_123", "name": "calculator", "arguments": {"expr": "2+2"}}]},
  {"role": "tool", "parts": [{"type": "tool_call_response", "id": "call_123", "response": "4"}]}
]
```

Part types: `text`, `tool_call`, `tool_call_response`, `server_tool_call`,
`server_tool_call_response`, `blob`, `file`, `uri`, `reasoning`, `compaction`, `generic`.

## `gen_ai.operation.name` Enum Values

`chat`, `generate_content`, `text_completion`, `embeddings`, `retrieval`,
`create_agent`, `invoke_agent`, `execute_tool`, `invoke_workflow`, `plan`,
`search_memory`, `create_memory`, `update_memory`, `upsert_memory`,
`delete_memory`, `create_memory_store`, `delete_memory_store`

## `gen_ai.provider.name` Enum Values

`openai`, `gcp.gen_ai`, `gcp.vertex_ai`, `gcp.gemini`, `anthropic`, `cohere`,
`azure.ai.inference`, `azure.ai.openai`, `ibm.watsonx.ai`, `aws.bedrock`,
`perplexity`, `x_ai`, `deepseek`, `groq`, `mistral_ai`, `moonshot_ai`
