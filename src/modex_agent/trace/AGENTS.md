<!-- Parent: ../AGENTS.md -->
<!-- Updated: 2026-08-26 -->

# trace

## Purpose

OTel-native observability layer for all agents. Emits OpenTelemetry-compatible
spans (`gen_ai.*` semantic conventions) via OTLP to a local OTel Collector →
Langfuse by default (`otel_http`); local JSONL (`spans.jsonl`) is retained as
a dormant FILE-mode fallback. Also provides cassette-based deterministic
replay and training-data derivation.

## Key Files

Span emission is decomposed into 7 specialized hook classes (ADR-0024 IN18),
assembled by `build_trace_hooks()` from the `trace_spans` tier
(`minimal`/`standard`/`full`).

| File | Description |
|------|-------------|
| `__init__.py` | Public API — exports the 7 span hooks, `BaseTraceHook`, `build_trace_hooks`, `TraceSessionState` + `MetricCounters`, cassette types (`CassetteRecorder`/`CassetteReplayEngine`), store/scoring/injector types |
| `factory.py` | `build_trace_hooks()` — assembles the specialized span hooks from `ObservabilityConfig` per the `trace_spans` tier (`minimal` = root only, `standard` = +chat/tool/handoff/approval, `full` = all 7); registration order is execution order; one `TraceSessionState` shared across all hooks |
| `base_hook.py` | `BaseTraceHook` — concrete shared base for the span hooks (span construction, scalar counter accumulation via `session.accumulate_span` after the store-None guard, persistence, attribute helpers; not an ABC) |
| `session_state.py` | `TraceSessionState` — per-`trace_id` mutable state shared by every hook from one `build_trace_hooks()` call (root span info, handoff span IDs); also owns the `MetricCounters` scalar accumulators (`accumulate_span` / `read_metrics` → `TrajectoryMetrics`, two-level `dict[trace_id, dict[root_span_id, ...]]`) that replace span read-back for L2 metrics; cleanup centralized in `clear_trace` (pops the counters bucket too) |
| `root_span_hook.py` | `RootSpanHook` — `invoke_agent` root span: pre-registered at `start_node_turn`, emitted complete at `finally_graph` (v4 immutability fix, IN18); `finally_graph` then derives `TrajectoryMetrics` from the counters (`read_metrics` — never reads spans back), stashes them on `TurnCustomKey.TRAJECTORY_METRICS` before `clear_trace`, and schedules fire-and-forget L2 score injection (IN19) — **injection skips non-`COMPLETED` turns** (failed/cancelled turns get no capability score; their stop_reason is already in spans for the histogram; the stash happens on every outcome); `ClosableHook.aclose()` blocks new injections, drains pending ones, closes the injector |
| `chat_span_hook.py` | `ChatSpanHook` — `chat` span per LLM call (`before_llm`/`after_llm_response`): model, prompt capture via `PromptCaptureStrategy`; usage consumed as typed `TokenUsage` attributes (semantic key normalization lives in the `TokenUsage` validator) |
| `tool_span_hook.py` | `ToolSpanHook` — `execute_tool_batch` + `execute_tool` spans (before/after tool execution) |
| `handoff_span_hook.py` | `HandoffSpanHook` — `agent.handoff` span at subagent dispatch; stores the handoff span ID the child root parents to |
| `approval_span_hook.py` | `ApprovalSpanHook` — `human.review` span (decision, deny reason, triggering tool) |
| `agent_start_hook.py` | `AgentStartSpanHook` — `agent.start` span at `start_node_turn` (full tier): system prompt text/hash/length + tool definitions |
| `iteration_span_hook.py` | `IterationSpanHook` — symmetric `iteration.start`/`iteration.end` pair per ReAct iteration (full tier) |
| `scoring.py` | Trajectory metrics — 12 direction-clear fields (tool_success_rate, tool_call_count, error_tool_count, iteration_count, llm_call_count, total_input_tokens, total_output_tokens, total_reasoning_tokens, api_latency_avg_s, cache_hit_rate, response_token_ratio, has_reasoning); `compute_metrics(spans)` is the span-based derivation (training exporter; also the parity reference the counters must match field-for-field); `compute_root_subtrees()` extracts per-root span subtrees (stops at nested `invoke_agent` roots) used by eval-side `metrics.py` for population-consistent aggregation — no longer called from `RootSpanHook` |
| `score_injector.py` | `L2ScoreInjector` — posts NUMERIC scores to the Langfuse ingestion API (`POST {host}/api/public/ingestion`); `inject_scores(trace_id, metrics: TrajectoryMetrics, *, observation_id)` takes derived metrics, never spans; one lazily-created resident `httpx.AsyncClient` (reused across injects, replaced when closed or cross-loop, idempotent `aclose()` with bounded in-flight drain); fire-and-forget, failures warning-only |
| `prompt_capture.py` | `PromptCaptureStrategy` ABC + `Off`/`Hash`/`Summary` (default)/`Full` implementations producing `gen_ai.input.*` attributes (ADR-0024 IN11); multimodal parts render via the shared `render_content_part_ref` bracket lines — span attributes carry refs, never base64 payloads |
| `cassette.py` | `CassetteRecorder` + `CassetteReplayEngine` — content-addressed LLM/tool capture for bit-identical replay; the replay engine exposes a `misses` counter to gate replays (lookup misses surface as error stops inside `ReActAgent.run`, not exceptions); records sanitize inline media payloads to sha256 digest placeholders (`[media sha256=…, data:<mime>, <n> bytes]`) — the call key hashes the ORIGINAL messages so replay keys stay stable, and `media://` refs pass through untouched |
| `training_exporter.py` | `TrainingDataExporter` — derives SFT OpenAI messages JSONL + DPO preference-pair JSONL from traced spans. L2 scoring, 3-tier dedup, scope-aware filtering |
| `otel_store.py` | `OtelSpanTraceStore` — backend-gated persistence: FILE = `spans.jsonl` append; OTEL_HTTP = **write-only** — bounded export queue (`export_queue_size=10000`, drop-oldest + counted on Full) drained by a daemon sender thread (httpx, 3 s timeout) that POSTs OTLP JSON; NO read buffers (the former per-session LRU was deleted 2026-08-18) — `list_by_session`/`list_by_trace_id` raise `NotImplementedError` in this mode (read traces via `LangfuseTraceQuery`); the hot path never touches the network. `build_trace_stores()` factory with config-driven selection + fall-back-to-FILE guard (missing `[observability]` extra / empty headers) |
| `langfuse_query.py` | `LangfuseClient` + `LangfuseTraceQuery` — cross-process read path over the Langfuse v2 API (`v2/observations`, cursor pagination, mandatory heavy-field `fields=` projection; `list_sessions` helper over `v2/sessions`). `observation_to_span()` reverse-normalizes observations back into `SpanModel`: TOOL → `execute_tool` name restore, metadata-first attribute rebuild (`attributes.*` keys authoritative over native fields), `{"result": ...}` envelope unwrap for tool output |
| `store.py` | `SpanModel`/`SpanStatus` (frozen Pydantic), `TraceQuery` ABC (read-only: `list_by_session`/`list_by_trace_id`), `JsonlSpanQuery` (pure-read impl) |
| `semconv.py` | `GenAiAttr`/`SpanName`/`SpanKind`/`SpanStatusCode` StrEnums centralizing all `gen_ai.*` attribute names and span name mappings |

## Subdirectories

None — flat module (19 source files + `__init__.py`).

## For AI Agents

### Data Flow

```
build_trace_hooks() → tier-selected span hooks (IN18)
    ↓ BaseTraceHook._save_span   (µs-scale hot path; no-op when store is None)
        ├─ session.accumulate_span → MetricCounters   (scalar counters, O(1),
        │    keyed by the turn's ROOT_SPAN_ID — never span objects)
        └─ store.save_span(SpanModel) → OtelSpanTraceStore
            ├─ FILE (dormant fallback): write spans.jsonl (local, agent self-read)
            │   → {base_dir}/{session_id}/spans.jsonl
            └─ OTEL_HTTP (default, write-only): bounded export queue ONLY
                → daemon sender thread → OTLP HTTP
                → collector (contrib 0.158.0) → Langfuse (system of record)

RootSpanHook.finally_graph
    → session.read_metrics(trace_id, root_span_id) → TrajectoryMetrics
    → stash ctx.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]
    → fire-and-forget score injection (COMPLETED turns only)
    → session.clear_trace(trace_id)   (pops the counters bucket)

Readers — same-process metrics: counters / TRAJECTORY_METRICS stash;
          cross-process spans: LangfuseTraceQuery (Langfuse v2 API);
          FILE-mode spans: JsonlSpanQuery (spans.jsonl).
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
- `trace_backend=OTEL_HTTP` (default) exports via the daemon sender thread;
  no local jsonl is written in this mode.
- `trace_backend=FILE` writes local JSONL only and never touches the
  network — `otel_endpoint` / `otel_headers` are ignored in this mode.
- The save path never raises and never blocks on the network (R1–R6
  resilience contract, see Tracing Data Path below).
- OTel SDK import is lazy (inside `build_trace_stores`), not at module level.
- `TraceSessionState.clear_trace` cleans up per-trace state in `finally_graph`
  (no memory leak).
- **OTEL_HTTP is write-only**: the store exposes no read path —
  `list_by_session`/`list_by_trace_id` raise `NotImplementedError`. Same-process
  metric consumers read the counters/stash; cross-process consumers use
  `LangfuseTraceQuery`. Never re-introduce a read buffer "for convenience".
- **Accumulators are SCALARS ONLY** (int/float counters, two-level
  `dict[trace_id, dict[root_span_id, MetricCounters]]`, ~80 bytes/trace, O(1)
  time/space per span). Never accumulate span objects, messages, or prompts —
  that is a buffer. `clear_trace` pops the trace bucket.
- **ClosableHook lifecycle**: `RootSpanHook` implements `ClosableHook`;
  `AgentPipeline.stop()` → `HookRunner.aclose()` → `RootSpanHook.aclose()`
  blocks new score scheduling, drains the strong-referenced
  `_pending_injections`, then closes the injector's resident client.

### Query Interface

- `OtelSpanTraceStore.list_by_session(session_id)` / `list_by_trace_id(trace_id)`
  — FILE only (session jsonl read / cross-session scan); OTEL_HTTP raises
  `NotImplementedError("OTEL_HTTP store is write-only; use LangfuseTraceQuery")`.
- Same-process metrics never touch the store: `TraceSessionState.read_metrics`
  (counters) during the turn, `TurnCustomKey.TRAJECTORY_METRICS` (frozen
  `TrajectoryMetrics` stash on the turn context) after `finally_graph`.
- `JsonlSpanQuery` — pure-read jsonl implementation (dormant FILE mode).
- `LangfuseTraceQuery` — cross-process read path over the Langfuse v2 API
  (otel_http mode; training export, curation).
- `TraceQuery` ABC read implementations: `JsonlSpanQuery` and
  `LangfuseTraceQuery` (the store's FILE branches remain a third read surface;
  its OTEL_HTTP side is write-only).

### Configuration

See `ObservabilityConfig` in `ioc/configs/observability.py`:
`trace_backend`, `otel_endpoint`, `otel_service_name`,
`retain_reasoning_content`, `checkpoint_per_iteration`,
`cassette_enabled`, `cassette_scope`, `training_relevant`,
`training_max_iterations`, `training_max_tokens`.

### Tracing Data Path (shipped 2026-08-17: OTel-only default, dormant jsonl fallback; amended 2026-08-18: write-only store + counters)

The former dual-write (ADR-0024 D6: every backend writes local
`spans.jsonl`) is superseded: the active path is OTel-only — app →
OTel Collector (contrib 0.158.0, retry/buffer) → Langfuse, the system of
record for `otel_http` mode. `TraceBackend.FILE` + `JsonlSpanQuery` remain
as a **dormant legacy mode** (selectable fallback, ADR-0007 — not
deleted). `TraceQuery` ABC keeps two read implementations (JsonlSpanQuery /
LangfuseTraceQuery) — the flexible switch point.
Full design + drill evidence: `docs/design/otel-collector/PRD.md`;
ADR-0024 D6/IN12/IN16 are amended in place.

**Emission (non-blocking)**: `save_span` is a µs-scale hot path — reasoning
strip, then either the FILE jsonl append (unchanged) or, for `otel_http`, a
`queue.Queue(maxsize=10000)` `put_nowait` (drop-oldest + counted on Full)
— no session buffers of any kind. A single daemon sender thread owns every
network call (httpx client, 3 s timeout) and POSTs OTLP JSON; a slow or
down collector stalls the sender, never the agent. `close()` = best-effort
flush ≤ 2 s. In parallel, `BaseTraceHook._save_span` folds each span into
the session's scalar `MetricCounters` (O(1) time/space, ~80 bytes/trace)
so metrics never require read-back.

**Read path**: same-process readers read metrics, not spans — the counters
(`read_metrics`) during the turn, the `TurnCustomKey.TRAJECTORY_METRICS`
stash after `finally_graph` (eval turn aggregation, training-data token
gate); cross-process readers (training export, curation) use
`LangfuseTraceQuery` — Langfuse `v2/observations` (cursor pagination,
heavy `fields=` projection) reverse-normalized by `observation_to_span()`:
TOOL observations restored to `execute_tool` (Langfuse promotes
`gen_ai.tool.name` to the observation name), metadata `attributes.*`
authoritative over native fields (verbatim usage/model/prompt/completion),
and the emitter's `{"result": ...}` envelope on TOOL output unwrapped back
into `gen_ai.tool.call.result`.

**Modes**:

| `trace_backend` | jsonl write | OTLP export | Read path |
|---|---|---|---|
| `otel_http` (default) | no | sender thread → collector → Langfuse | same-process metrics: counters / `TRAJECTORY_METRICS` stash; cross-process spans: `LangfuseTraceQuery` |
| `file` (dormant, ADR-0007) | yes | no | `JsonlSpanQuery` |
| `off` | no | no | none |

**Resilience contract** (drill-verified 2026-08-17; PRD degradation matrix):

- R1 `trace_backend=off` — agent runs normally, nothing emitted.
- R2 collector refused — turn unaffected, spans dropped at sender + counted.
- R3 collector hanging — turn unaffected, sender times out (3 s), drops, keeps draining.
- R4 Langfuse down, collector up — turn unaffected, collector buffers + redelivers.
- R5 long outage, queue full — bounded memory, oldest spans dropped + counted.
- R6 shutdown with queued spans — clean exit, best-effort flush ≤ 2 s.

**Future work (not yet implemented)**:

1. `export-training` CLI command (in `bot.eval.cli`) — session
   auto-discovery via Langfuse `v2/sessions` + `TrainingDataExporter`
   over `LangfuseTraceQuery`.
2. ~~Retention for otel_http mode = Langfuse/ClickHouse TTL configuration~~ —
   resolved 2026-08-18: 180-day ClickHouse TTL on the five trace tables +
   MinIO lifecycle on `events/` + system-log opt-out; ops runbook in
   `examples/bot_project/docs/langfuse/langfuse-deployment.md` §10.

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
