<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-15 -->

# trace

## Purpose

OTel-native observability layer for all agents. Emits OpenTelemetry-compatible
spans (`gen_ai.*` semantic conventions) to local JSONL files (`spans.jsonl`)
and optionally to remote OTLP backends (Langfuse/Phoenix/Datadog). Also
provides cassette-based deterministic replay and training-data derivation.

## Key Files

Span emission is decomposed into 7 specialized hook classes (ADR-0024 IN18),
assembled by `build_trace_hooks()` from the `trace_spans` tier
(`minimal`/`standard`/`full`).

| File | Description |
|------|-------------|
| `__init__.py` | Public API — exports the 7 span hooks, `BaseTraceHook`, `build_trace_hooks`, `TraceSessionState`, cassette types (`CassetteRecorder`/`CassetteReplayEngine`), store/scoring/injector types |
| `factory.py` | `build_trace_hooks()` — assembles the specialized span hooks from `ObservabilityConfig` per the `trace_spans` tier (`minimal` = root only, `standard` = +chat/tool/handoff/approval, `full` = all 7); registration order is execution order; one `TraceSessionState` shared across all hooks |
| `base_hook.py` | `BaseTraceHook` — concrete shared base for the span hooks (span construction, persistence, attribute helpers; not an ABC) |
| `session_state.py` | `TraceSessionState` — per-`trace_id` mutable state shared by every hook from one `build_trace_hooks()` call (root span info, handoff span IDs); cleanup centralized in `clear_trace` |
| `root_span_hook.py` | `RootSpanHook` — `invoke_agent` root span: pre-registered at `start_node_turn`, emitted complete at `finally_graph` (v4 immutability fix, IN18); also runs subtree-scoped L2 score injection after root persistence (IN19) — **skips non-`COMPLETED` turns** (failed/cancelled turns get no capability score; their stop_reason is already in spans for the histogram) |
| `chat_span_hook.py` | `ChatSpanHook` — `chat` span per LLM call (`before_llm`/`after_llm_response`): model, usage, prompt capture via `PromptCaptureStrategy` |
| `tool_span_hook.py` | `ToolSpanHook` — `execute_tool_batch` + `execute_tool` spans (before/after tool execution) |
| `handoff_span_hook.py` | `HandoffSpanHook` — `agent.handoff` span at subagent dispatch; stores the handoff span ID the child root parents to |
| `approval_span_hook.py` | `ApprovalSpanHook` — `human.review` span (decision, deny reason, triggering tool) |
| `agent_start_hook.py` | `AgentStartSpanHook` — `agent.start` span at `start_node_turn` (full tier): system prompt text/hash/length + tool definitions |
| `iteration_span_hook.py` | `IterationSpanHook` — symmetric `iteration.start`/`iteration.end` pair per ReAct iteration (full tier) |
| `scoring.py` | Trajectory metrics — 12 direction-clear fields (tool_success_rate, tool_call_count, error_tool_count, iteration_count, llm_call_count, total_input_tokens, total_output_tokens, total_reasoning_tokens, api_latency_avg_s, cache_hit_rate, response_token_ratio, has_reasoning) computed from chat/tool/iteration spans; shared by the training exporter and the score injector; `compute_root_subtrees()` extracts per-root span subtrees (stops at nested `invoke_agent` roots) used by both `RootSpanHook` injection and eval-side `metrics.py` for population-consistent aggregation |
| `score_injector.py` | `L2ScoreInjector` — posts NUMERIC scores to the Langfuse ingestion API (`POST {host}/api/public/ingestion`); fire-and-forget, failures warning-only |
| `prompt_capture.py` | `PromptCaptureStrategy` ABC + `Off`/`Hash`/`Summary` (default)/`Full` implementations producing `gen_ai.input.*` attributes (ADR-0024 IN11) |
| `cassette.py` | `CassetteRecorder` + `CassetteReplayEngine` — content-addressed LLM/tool capture for bit-identical replay; the replay engine exposes a `misses` counter to gate replays (lookup misses surface as error stops inside `ReActAgent.run`, not exceptions) |
| `training_exporter.py` | `TrainingDataExporter` — derives SFT OpenAI messages JSONL + DPO preference-pair JSONL from traced spans. L2 scoring, 3-tier dedup, scope-aware filtering |
| `otel_store.py` | `OtelSpanTraceStore` — writes `spans.jsonl` + optional OTLP export via OTel SDK `Tracer`. `build_trace_stores()` factory with config-driven selection + `ImportError` guard for `[observability]` extra |
| `store.py` | `SpanModel`/`SpanStatus` (frozen Pydantic), `TraceQuery` ABC (read-only: `list_by_session`/`list_by_trace_id`), `JsonlSpanQuery` (pure-read impl) |
| `semconv.py` | `GenAiAttr`/`SpanName`/`SpanKind`/`SpanStatusCode` StrEnums centralizing all `gen_ai.*` attribute names and span name mappings |
| `hooks.py` | Empty module — the former all-in-one `TraceCollectorHook` was removed by the IN18 7-hook decomposition; no live importers |

## Subdirectories

None — flat module (19 source files + `__init__.py`; `hooks.py` is an empty leftover).

## For AI Agents

### Data Flow

```
build_trace_hooks() → tier-selected span hooks (IN18)
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
    ├─ agent.handoff (INTERNAL) — subagent dispatch; child root parents to it
    ├─ agent.start (INTERNAL, full tier) — system prompt + tool definitions
    └─ iteration.start / iteration.end (INTERNAL, full tier) — ReAct round boundaries
```

`TraceSessionState.root_span_info` is seeded by `RootSpanHook` at
`start_node_turn` (pre-registration) and consumed at `finally_graph` (single
complete root-span write + cleanup). Child spans reference the root via
`parent_span_id`; subagent roots reuse the inherited `trace_id` and parent to
the dispatching `agent.handoff` span.

### Design Rules

- `reasoning_content` retained in trace when `retain_reasoning_content=True`;
  stripped in `OtelSpanTraceStore.save_span()` when `False`.
- `trace_backend=OFF` disables all span emission (zero overhead).
- `trace_backend=FILE` (default) writes local JSONL only.
- `otel_endpoint` set → concurrent local + remote OTLP export.
- OTel SDK import is lazy (inside `build_trace_stores`), not at module level.
- `TraceSessionState.clear_trace` cleans up per-trace state in `finally_graph`
  (no memory leak).

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
