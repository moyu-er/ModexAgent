<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-06-22 -->

# trace

## Purpose

OTel-native observability layer for all agents. Emits OpenTelemetry-compatible
spans (`gen_ai.*` semantic conventions) to local JSONL files (`spans.jsonl`)
and optionally to remote OTLP backends (Langfuse/Phoenix/Datadog). Also
provides cassette-based deterministic replay and training-data derivation.

## Key Files

| File | Description |
|------|-------------|
| `store.py` | `SpanModel`/`SpanStatus` (frozen Pydantic), `TraceQuery` ABC (read-only: `list_by_session`/`list_by_trace_id`), `JsonlSpanQuery` (pure-read impl) |
| `otel_store.py` | `OtelSpanTraceStore(TraceQuery)` — writes `spans.jsonl` + optional OTLP export via OTel SDK `Tracer`. `build_trace_stores()` factory with config-driven selection + `ImportError` guard for `[observability]` extra |
| `semconv.py` | `GenAiAttr`/`SpanName`/`SpanKind`/`SpanStatusCode` StrEnums centralizing all `gen_ai.*` attribute names and span name mappings |
| `hooks.py` | `TraceCollectorHook` — implements 5 hook ABCs, constructs `SpanModel` directly, maintains `_root_span_info` for parent linking |
| `cassette.py` | `CassetteRecorder` (wraps LLM provider + tool dispatcher for bit-identical replay), `CassetteReplayEngine`, `CassetteFlushHook(FinallyGraphHook)`, content-addressed storage |
| `training_exporter.py` | `TrainingDataExporter` — derives SFT OpenAI messages JSONL + DPO preference-pair JSONL from traced spans. L2 scoring, 3-tier dedup, scope-aware filtering |

## Subdirectories

None — flat module (6 source files + `__init__.py`).

## For AI Agents

### Data Flow

```
TraceCollectorHook (lifecycle events)
    ↓ save_span(SpanModel) → OtelSpanTraceStore
    ↓
OtelSpanTraceStore
    ├─ Primary: write spans.jsonl (local, agent self-read)
    │   → {base_dir}/{session_id}/spans.jsonl
    └─ Secondary: emit via OTel SDK (optional, when otel_endpoint set)
        → OTLP HTTP → Langfuse/Phoenix/Datadog
```

### Span Tree Construction

```
invoke_agent (INTERNAL, parent_span_id=null)
    ← written at finally_graph with full duration + stop_reason + error
    │
    ├─ chat (CLIENT) — each LLM call
    ├─ execute_tool_batch (INTERNAL) — tool batch start
    ├─ execute_tool (INTERNAL) — per-tool result
    ├─ human.review (INTERNAL) — approval decisions
    └─ training_tag (INTERNAL) — gen_ai.training.relevant flag
```

`_root_span_info: dict[trace_id, (span_id, start_time)]` is set at
`before_graph` (pre-registration) and consumed at `finally_graph` (root span
write + cleanup). Child spans reference the root via `parent_span_id`.

### Design Rules

- `reasoning_content` retained in trace when `retain_reasoning_content=True`;
  stripped in `OtelSpanTraceStore.save_span()` when `False`.
- `trace_backend=OFF` disables all span emission (zero overhead).
- `trace_backend=FILE` (default) writes local JSONL only.
- `otel_endpoint` set → concurrent local + remote OTLP export.
- OTel SDK import is lazy (inside `build_trace_stores`), not at module level.
- `_root_span_info` is cleaned up in `finally_graph` (no memory leak).

### Query Interface

- `OtelSpanTraceStore.list_by_session(session_id)` — spans from `spans.jsonl`.
- `OtelSpanTraceStore.list_by_trace_id(trace_id)` — cross-session search.
- `JsonlSpanQuery` — pure-read implementation (no write capability).

### Configuration

See `ObservabilityConfig` in `ioc/configs/observability.py`:
`trace_backend`, `otel_endpoint`, `otel_service_name`,
`retain_reasoning_content`, `checkpoint_per_iteration`,
`cassette_enabled`, `cassette_scope`, `training_relevant`,
`training_max_iterations`, `training_max_tokens`.

## Dependencies

### Internal
- `modex_agent.hook.abc` — hook ABCs
- `modex_agent.runtime.enums` — `OperationKind`, `TurnCustomKey`
- `modex_agent.ioc.configs.observability` — `ObservabilityConfig`, `TraceBackend`
- `modex_agent.utils.file_io` — `read_jsonl_robust`

### External
- `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` — optional
  (`[observability]` extra), required only for OTLP remote export

<!-- MANUAL: -->
