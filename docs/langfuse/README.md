# Langfuse OTLP Attribute Mapping — Reference

> Source: [Langfuse OpenTelemetry docs](https://langfuse.com/docs/opentelemetry/get-started)
> Pulled: 2026-07-27
> Langfuse v3.224.1 (self-hosted)

## Endpoint

```
OTLP HTTP:  {LANGFUSE_HOST}/api/public/otel/v1/traces
Auth:       Authorization: Basic base64(public_key:secret_key)
Header:     x-langfuse-ingestion-version: 4  (real-time ingestion)
Protocols:  HTTP/protobuf AND HTTP/JSON (gRPC NOT supported)
```

## Trace-Level Attributes (set on ANY span → applies to whole trace)

| Langfuse Field | Mapped from OTel Attribute | Notes |
|----------------|---------------------------|-------|
| `name` | `langfuse.trace.name` OR root span name | |
| `userId` | `langfuse.user.id` OR `user.id` | |
| `sessionId` | `langfuse.session.id` OR `session.id` | |
| `release` | `langfuse.release` | |
| `public` | `langfuse.trace.public` | boolean |
| `tags` | `langfuse.trace.tags` | string[] |
| `metadata` | `langfuse.trace.metadata.*` | top-level filterable |
| `input` | `langfuse.trace.input` OR root span's observation input | deprecated in v4 |
| `output` | `langfuse.trace.output` OR root span's observation output | deprecated in v4 |
| `version` | root span's attributes | |
| `environment` | root span's attributes | |

**Important**: `langfuse.*` attributes MUST be on EVERY span (not just root)
for reliable filtering/aggregation. Use OTel Baggage + BaggageSpanProcessor
for propagation.

## Observation-Level Attributes (per-span)

| Langfuse Field | Mapped from OTel Attribute | Notes |
|----------------|---------------------------|-------|
| `type` | `langfuse.observation.type` (`span`/`generation`/`event`) | Default: `span`. Any span with `model` attr → `generation` |
| `level` | `langfuse.observation.level` OR inferred from `span.status.code` | `DEBUG`/`DEFAULT`/`WARNING`/`ERROR` |
| `statusMessage` | `langfuse.observation.status_message` OR `span.status.message` | |
| `metadata` | `langfuse.observation.metadata.*` | top-level filterable |
| **`input`** | `langfuse.observation.input` OR **`gen_ai.prompt`** OR `input.value` OR `mlflow.spanInputs` | `(JSON) string` |
| **`output`** | `langfuse.observation.output` OR **`gen_ai.completion`** OR `output.value` OR `mlflow.spanOutputs` | `(JSON) string` |
| `model` | `langfuse.observation.model.name` OR `gen_ai.request.model` OR `gen_ai.response.model` OR `llm.model_name` OR `model` | |
| `modelParameters` | `langfuse.observation.model.parameters` OR `gen_ai.request.*` | JSON string |
| `usage` | `langfuse.observation.usage_details` OR **`gen_ai.usage.*`** | JSON string |
| `cost` | `langfuse.observation.cost_details` OR `gen_ai.usage.cost` | |
| `prompt` | `langfuse.observation.prompt.name` + `langfuse.observation.prompt.version` | |
| `completionStartTime` | `langfuse.observation.completion_start_time` | ISO 8601 |

## Critical Gap: OTel Standard vs Langfuse Mapping

Langfuse maps `input`/`output` from:
- `gen_ai.prompt` / `gen_ai.completion` (OLD OTel draft names, Langfuse legacy)
- `langfuse.observation.input` / `langfuse.observation.output` (Langfuse native)

Langfuse does NOT map from current OTel standard names:
- `gen_ai.input.messages` → goes to `metadata.attributes` (NOT `input`)
- `gen_ai.output.messages` → goes to `metadata.attributes` (NOT `output`)

**Solution**: Emit BOTH:
1. OTel standard `gen_ai.input.messages` / `gen_ai.output.messages` (for standards compliance)
2. Langfuse `gen_ai.prompt` / `gen_ai.completion` (for Langfuse input/output fields)

The `gen_ai.prompt` / `gen_ai.completion` are JSON-serialized strings of the
standard messages format.

## Unmapped Attributes

All OTel attributes NOT in the mapping tables above go to:
- `metadata.attributes.*` (catch-all, NOT filterable in Langfuse UI)

To make an attribute filterable, prefix it with:
- `langfuse.trace.metadata.*` (trace-level)
- `langfuse.observation.metadata.*` (observation-level)

## Resource Attributes

Go to `metadata.resourceAttributes.*`:
- `service.name` → `metadata.resourceAttributes.service.name`
- `telemetry.sdk.*` → `metadata.resourceAttributes.telemetry.sdk.*`

## Security

- Attribute keys containing `__proto__`, `constructor`, or `prototype` as a
  path segment are silently dropped (prototype pollution prevention)
- `gen_ai.input.messages` / `gen_ai.output.messages` / `gen_ai.system_instructions`
  may contain PII — mark as opt-in in instrumentation

## ModexAgent Implementation

### Observation Type Mapping

| Span name | `langfuse.observation.type` | Langfuse type |
|-----------|----------------------------|---------------|
| `invoke_agent` | `agent` | AGENT |
| `chat` | `generation` | GENERATION |
| `execute_tool` | `tool` | TOOL |
| `iteration` | `span` | SPAN |
| `human_review` | `event` | EVENT |
| `agent.handoff` | `span` | SPAN |

### Trace Name

`langfuse.trace.name` = `{session_id}.{turn_id}` — set on all spans so the
trace name is populated regardless of which span arrives first at Langfuse.

### Root Span Emission

`invoke_agent` is emitted twice with the same `span_id`:
1. `before_turn`: start + input (trigger message) + `as_root=true`
2. `finally_turn`: output (final reply) + end_time + aggregated usage

Langfuse merges both by `span_id` — first emission's input + second's output.

### Export Path

Direct JSON OTLP HTTP POST (bypasses OTel SDK). See `docs/otel/README.md`.
