# OTel Collector Migration — OTel-Only Default, Dormant jsonl Fallback

Status: implemented (2026-08-17) — design v4; supersedes v1-v3.
Evidence: per-ticket ledger in `.omo/notepads/otel-collector-migration/learnings.md`; gate reports in `.omo/start-work/fidelity-report.md` and `.omo/start-work/e2e-equivalence-report.md`.
Decision owner: project owner, 2026-08-17:

1. Active trace path becomes **OTel-only**: app → collector (contrib
   0.158.0, pinned) → Langfuse. Collector is the reliability path
   (retry/buffer), replacing jsonl's outage-insurance role.
2. Local `spans.jsonl`: **retained as a dormant legacy mode** —
   `TraceBackend.FILE` enum value, file-write code, and `JsonlSpanQuery`
   all stay (ADR-0007: keep zero-usage modules with real seams). Selectable,
   not default, not deleted.
3. Subagent notification no longer carries the `Trace:` path
   (`SubagentAutoSendHook` / `ResultMeta.trace_path` removed — not injected,
   not used).
4. **`TraceQuery` ABC is the flexible switch point** — three
   implementations coexist (see Modes table).
5. **Resilience Requirement (hard)**: the agent must run independently of
   the entire telemetry stack — OTel off / collector down / hanging /
   Langfuse down must never block, crash, or measurably stall an agent
   turn (R1–R6 contract below).

> **Status addendum (2026-08-18, write-only refactor —
> `.omo/plans/trace-write-only-refactor.md`)**: partially superseded. The
> in-process store buffers described below (per-session `deque(maxlen=4096)`
> LRU, "in-process memory buffer" read paths) were REMOVED — the OTEL_HTTP
> store is write-only (queue + sender only; `list_by_session` /
> `list_by_trace_id` raise `NotImplementedError`). Same-process metrics now
> come from scalar `MetricCounters` + the per-turn `TRAJECTORY_METRICS`
> stash; cross-process reads remain `LangfuseTraceQuery` only. Open
> question 3 is resolved: retention = 180-day ClickHouse TTL + MinIO
> `events/` lifecycle (ops runbook: `langfuse-deployment.md` §10). The
> resilience contract, collector configuration, and FILE fallback below
> remain authoritative.

## Modes

| `trace_backend` | jsonl write | OTLP export | Read path |
|---|---|---|---|
| `otel_http` (**new default**) | no | app → collector → Langfuse | in-process memory buffer (same-process); `LangfuseTraceQuery` (cross-process) |
| `file` (dormant legacy) | yes | no | `JsonlSpanQuery` (unchanged code + tests) |
| `off` | no | no | none |

## Resilience Requirement

The agent must run independently of the entire telemetry stack. Telemetry
degradation may lose data; it may never block, crash, or measurably stall
an agent turn.

**Audited blocking finding (drives the core ticket):**
`_emit_span_via_json_otlp` (`src/modex_agent/trace/otel_store.py:118-128,
356-425`) is a synchronous httpx POST (timeout 10.0 s) inline in
`save_span` — a hanging collector stalls the event loop up to 10 s per
span (200 s for a 20-span turn). This must leave the hot path before
`otel_http` becomes the default.

Degradation matrix (hard contract):

| # | Scenario | Agent must | Data must |
|---|---|---|---|
| R1 | `trace_backend=off` | run normally, zero trace network | nothing emitted (exists) |
| R2 | collector refused | normal; <100ms/turn overhead; warnings only | spans dropped at sender; drop counter |
| R3 | collector hanging (black hole) | turn wall-clock unaffected | sender timeout ≤3s, drop, keep draining |
| R4 | Langfuse down, collector up | normal | collector queue buffers, redelivers |
| R5 | long outage, queue full | normal, bounded memory | oldest dropped + counted, no OOM |
| R6 | shutdown with queued spans | clean exit, no close-hang | best-effort flush ≤2s |

Target architecture — hot path is µs-scale and decoupled from turn
wall-clock:

```
save_span (hot path, µs)
  ├─ reasoning-strip copy
  ├─ FILE: jsonl append (unchanged)
  └─ otel_http:
       ├─ per-session deque(maxlen=4096) buffer
       └─ queue.Queue(maxsize=10000) put_nowait — drop-oldest + count on Full

daemon sender thread — sync httpx, timeout 3.0 s — drains queue → OTLP POST
```

The sender thread owns every network call (R2/R3); the collector's
`sending_queue` + `retry_on_failure` own R4; bounded deque + queue own R5;
`close()` best-effort flush ≤2 s owns R6.

## Consumer Table (verified 2026-08-17)

| Consumer | Today | After | Change size |
|---|---|---|---|
| `OtelSpanTraceStore.save_span` | always appends jsonl + OTLP POST when otel_http | µs hot path (see Resilience Requirement): reasoning-strip copy → FILE: jsonl append (unchanged) / otel_http: per-session `deque(maxlen=4096)` + `queue.Queue(maxsize=10000)` put_nowait (drop-oldest on Full); daemon sender thread (httpx timeout 3.0 s) drains → OTLP; rate-limited warnings + drop counter; `close()` flush ≤2 s | core |
| OTel SDK tracer path (`_build_otlp_tracer` → `BatchSpanProcessor`) | batch export on SDK worker thread | unchanged (already non-blocking) | none |
| `TraceBackend.FILE` / `JsonlSpanQuery` | active default | dormant, intact | none |
| `LangfuseTraceQuery` (new) | — | `TraceQuery` impl over Langfuse `v2/observations` (+ `list_sessions` helper via `v2/sessions` for the future export-training CLI); query rules + refined mapping in Spec corrections #3 (mandatory `fields=` projection, cursor pagination, `metadata.attributes.*` rebuild); observation→`SpanModel` mapping (`input`→`gen_ai.prompt`, `output`→`gen_ai.completion`, `usage.*`→`gen_ai.usage.*`, `parentId`→`parent_span_id`) | new module |
| `TrainingDataExporter` | takes any `TraceQuery`; wired to `JsonlSpanQuery` | **code unchanged**; wiring binds `LangfuseTraceQuery` by default, `JsonlSpanQuery` when backend=FILE | wiring only |
| eval `experiment_runner` tool_stats (L167) | `JsonlSpanQuery` after run | in-process store buffer (`store.list_by_session`) — same instance the harness built | ~1 line |
| eval `metrics` CLI | glob `spans.jsonl` | **kept as-is** (reads legacy/FILE-mode data; historical files remain readable); new otel_http data already carries the 12 metrics as Langfuse scores — dashboards/`compare` are the primary view; optional `--source scores` deferred | none now |
| `SubagentAutoSendHook` | injects `Trace: <path>` into parent notification | `trace_enabled` param + trace_path block + `ResultMeta.trace_path` field + `message_format.py` render all removed | localized |
| `RootSpanHook` L2 score injection | posts 12 metrics to `{otel_endpoint host}/api/public/ingestion` | unchanged mechanism; **already compliant** (async 5 s fire-and-forget, failures warning-only); URL source split: new `ObservabilityConfig.eval_ingestion_url` (default: derive as today; collector mode: set explicitly), because `otel_endpoint` now points at the collector | small |
| cassette / golden replay | own files | unchanged | none |

## Fidelity Audit Gate (regression check)

The 2026-08-17 live drill verified all four former risk fields survive —
**fallbacks NOT needed** (evidence in Verification results below). The
gate remains as a regression check on the scripted-turn path: round-trip a
scripted turn (full prompt capture, reasoning, cache_read tokens, tool
attrs, approval decision, training-relevant tag) through collector →
Langfuse → `TrainingDataExporter` over `LangfuseTraceQuery`, assert
equivalence against the in-process buffer, and assert the `fields=`
projection + `metadata.attributes.*` survival.

Future `export-training` CLI binding: `TrainingDataExporter(LangfuseTraceQuery(LangfuseClient(...)))`; FILE mode keeps `JsonlSpanQuery`.

| Field | Verified survival (2026-08-17) |
|---|---|
| `gen_ai.usage.cache_read.input_tokens` | native map → `usageDetails.input_cached_tokens` |
| `gen_ai.output.reasoning_content` | `metadata.attributes.*` |
| `gen_ai.training.relevant` | `metadata.attributes.*` |
| `human.review` decision | `metadata.attributes.*` |

### Regression gate results (2026-08-17, live store path — ticket 6)

Scripted turn (6 spans, traceId prefix `666964656c697479` = "fidelity",
session `fidelity-gate-1`) pushed through the real `OtelSpanTraceStore`
(`backend=OTEL_HTTP`, daemon sender thread) → collector :4318 → Langfuse
4.11.0, read back via `LangfuseTraceQuery`, compared field-by-field against
the in-process buffer. All four risk fields pass; structural checks (id-keyed)
pass: 6/6 observations, span ids verbatim, parent tree intact, chat →
GENERATION, prompt/completion via observation input/output. Full evidence:
`.omo/start-work/fidelity-report.md`.

| Field | Verdict | Surfaced as |
|---|---|---|
| `gen_ai.usage.cache_read.input_tokens` (64) | pass | `usageDetails.input_cached_tokens` (native; verbatim copy also in `metadata.attributes.*`) |
| `gen_ai.output.reasoning_content` | pass | `metadata.attributes.*` (flat key) |
| `gen_ai.training.relevant` | pass | `metadata.attributes.*` (flat key) |
| `human.review` decision (`gen_ai.approval.decision`) | pass | `metadata.attributes.*` (flat key; `deny_reason` absent as sent) |

Documented Langfuse-native deltas (not gate failures; ticket-13
allowed-deltas list):

1. `execute_tool` observations return named after the tool — Langfuse's OTLP
   ToolMapper promotes `gen_ai.tool.name` to the observation name (type
   TOOL); name-filtered tool-span logic must match on `gen_ai.tool.name`
   metadata or span id, not span name.
2. `usageDetails.input` = sent `gen_ai.usage.input_tokens` − `cache_read`
   (120−64=56); the verbatim 120 survives in `metadata.attributes.*`, but
   `observation_to_span` maps the native value, so the read path returns 56.
3. `gen_ai.tool.call.result` surfaces as observation `output` (read back as
   `gen_ai.completion` on the tool span), not under its original key.

Golden capture for ticket 13 (pre-flip, legacy jsonl):
`.omo/start-work/golden-sft/` — `legacy_sft.jsonl` (21 SFT examples, 4
deduped) + `manifest.json` (exact session_ids, source base_dir, git commit,
selection policy) + `staged_sessions/` (the exact exporter input; ingest these
into Langfuse for the diff). Legacy data predates `TrainingDataHook` — zero
`training_tag` spans exist (`training_relevant` disabled in bot config) — so
hook-shaped tags were injected into the staged copy per the hook's L1 rules;
recorded in the manifest.

## Verification results (2026-08-17 live drill)

Standalone collector + synthetic spans against live Langfuse 4.11.0; zero
repo changes. All green — design confirmed viable end-to-end.

| Check | Verdict |
|---|---|
| Image `otel/opentelemetry-collector-contrib:0.158.0` boots, validates `sending_queue`/`retry_on_failure` exporter config | pass |
| App-style OTLP JSON POST → :4318 → batch → otlphttp → Langfuse, visible in ~12 s (ingestion-version header = real-time path) | pass |
| Trace tree fidelity: `parentObservationId` + span ids preserved **verbatim** (id == our span_id, zero translation) | pass |
| `gen_ai.usage.cache_read.input_tokens` **natively mapped** to `usageDetails.input_cached_tokens` (fallback NOT needed) | pass |
| `gen_ai.output.reasoning_content`, `gen_ai.training.relevant`, ALL unmapped `gen_ai.*` attrs survive under `metadata.attributes.*` (fallbacks NOT needed) | pass |
| `gen_ai.prompt`/`gen_ai.completion` → observation `input`/`output`; `gen_ai.conversation.id` → sessionId; AGENT/GENERATION type inference | pass |
| **R4 live drill**: `docker stop web` → POST spans (app gets 200 instantly, never blocked) → collector logs retry backoff → `docker start web` → both spans arrive (verified by traceId query) | pass |

### Spec corrections (verified 2026-08-17)

1. **Exporter name**: `otlphttp` is a **deprecated alias** in 0.158.0; use
   `otlp_http/langfuse` (warning logged otherwise).
2. **Mandatory `--config` flag**: the image default CMD points at
   `/etc/otelcol-contrib/config.yaml`, NOT `/etc/otelcol/config.yaml`; the
   explicit `command: ["--config=/etc/otelcol/config.yaml"]` is mandatory.
   Verified footgun: without it the container silently runs the built-in
   default config — receiver 200s, traces pipeline absent, spans dropped,
   zero errors logged.
3. **`v2/observations` query rules (major)**: default projection is
   `core,basic` only; heavy fields REQUIRE
   `fields=core,basic,io,usage,metadata,model`. Pagination is cursor-based
   (`meta.cursor`), NOT `page`; sort fixed startTime DESC; `limit` max
   1000; always bound queries with `fromStartTime`/`toStartTime`; no
   get-by-id route (filter on `id` column). `traceId` filter verified
   working. Mapping: `usageDetails.{input,output,total,input_cached_tokens}`
   native; all `gen_ai.*` attrs rebuildable from `metadata.attributes.*`;
   `parentObservationId` → parent_span_id; `id` → span_id. Caveat:
   `providedModelName` may be empty on the OTLP path; read model from
   `metadata.attributes.gen_ai.request.model`/`gen_ai.response.model`.
4. **`/v2/traces*` endpoints are gone** in events_only (404 with
   deprecation body pointing to v2/observations); anything planning to read
   traces (not observations) must group by traceId. Sessions endpoint for
   the future export-training CLI: untested, verify v4 sessions API shape
   when that ticket lands.
5. **15-min delay trap**: exporters NOT sending
   `x-langfuse-ingestion-version: "4"` see up to 15 min ingestion delay on
   v2 reads. Keep the header in every deployment mode (collector verified
   ~12 s; direct-POST path already sends it too).

## Deployment

Collector = **default compose service** (it is the reliability path):

```yaml
otel-collector:
  image: docker.xuanyuan.run/otel/opentelemetry-collector-contrib:0.158.0
  # MANDATORY (Spec correction #2): without this flag the image default CMD
  # silently loads /etc/otelcol-contrib/config.yaml — receiver 200s, no
  # traces pipeline, spans dropped, zero errors.
  command: ["--config=/etc/otelcol/config.yaml"]
  volumes: ["./otel-collector.yaml:/etc/otelcol/config.yaml:ro"]
  ports: ["127.0.0.1:4318:4318"]
  deploy: { resources: { limits: { memory: 128M, cpus: '0.5' } } }
  restart: unless-stopped
```

`otel-collector.yaml`: `otlp` receiver (4318) + `health_check` extension
(:13133, for the compose healthcheck) → `batch` processor → `otlp_http`
exporter (the `otlphttp` name is a deprecated alias, Spec correction #1) →
`http://langfuse-web:3000/api/public/otel` with
`Authorization: Basic ${pk:sk}` + `x-langfuse-ingestion-version: "4"`
(auth consolidated collector-side), `sending_queue` + `retry_on_failure`
enabled.

**Memory (3 GB cap)**: 3008M + 128M collector − 64M (minio 192→128M,
observed ~68M) = **3072M**.

**App config** (`bot_config.yml`):

```yaml
trace_backend: "${OTEL_FORMAT:-otel_http}"          # default flips to otel_http
otel_endpoint: "${OTEL_TRACES_ENDPOINT:-http://localhost:4318/v1/traces}"
eval_ingestion_url: "${LANGFUSE_HOST:-http://localhost:3000}/api/public/ingestion"
```

`.env`: `LANGFUSE_HOST` + pk/sk stay (score injector + Langfuse SDK +
curator are direct by design); `LANGFUSE_BASIC_AUTH` still consumed by the
injector and (optionally) as collector auth source. `OTEL_TRACES_ENDPOINT`
unset → collector default; set to
`${LANGFUSE_HOST}/api/public/otel/v1/traces` to bypass collector if ever
needed (another ABC-style flexibility point).

Paths NOT routed through the collector (unchanged): Langfuse SDK (eval
CLI run/curate/compare), `L2ScoreInjector` — Langfuse-native APIs, not OTLP.

## Rollout

Deployment-first: the collector goes in **inert** (no app traffic) before
any app change, so every later step verifies against a live pipeline.

1. Deploy collector (inert): compose service + `otel-collector.yaml` +
   minio rebalance; verify a real OTLP span via curl → 200 AND visible in
   Langfuse `v2/observations`.
2. Core non-blocking emission (`otel_store.py`): µs hot path + queue +
   daemon sender thread + `close()` flush; resilience tests R2/R3/R5 +
   eviction + FILE regression; fix tests broken by dual-write removal.
3. `LangfuseTraceQuery` + `eval_ingestion_url` seam (config field,
   factory/harness precedence wiring).
4. Fidelity regression gate on the live pipeline: scripted turn →
   collector → Langfuse → `LangfuseTraceQuery`; golden `export_sft`
   captured from legacy jsonl BEFORE the default flips.
5. Consumer swaps: `experiment_runner` buffer, exporter wiring,
   SubagentAutoSend/`message_format` `Trace:` removal.
6. Env switch (`bot_config.yml` default `otel_http`) + live drills
   R1/R2/R4; trace tree intact in Langfuse UI.
7. Test-suite alignment + docs closure: ADR-0024 D6/IN12/IN16 amended in
   place (jsonl → dormant fallback, collector = reliability path,
   Langfuse = system of record for otel_http mode); `trace/AGENTS.md`,
   `bot/eval/AGENTS.md`, `langfuse-deployment.md`.
8. E2E: `export_sft` schema+count equivalence vs the golden file.

Rollback: set `OTEL_FORMAT=file` + unset `OTEL_TRACES_ENDPOINT` → exact
legacy behavior (this is why FILE stays deletable-never).

## Effort

~2–3 days (up from 1.5–2): adds the core queue/sender rework + resilience
test matrix; the fidelity audit is now a regression check, not new
engineering. No code deletion, no mass test migration.

## Open questions

1. ~~xuanyuan mirror of collector 0.158.0~~ — resolved (pulled 2026-08-17).
2. ~~Unmapped `gen_ai.*` attribute survival in Langfuse observations~~ —
   resolved by live drill (Verification results; fallbacks not needed).
3. Langfuse 4.11 ClickHouse TTL default (retention) — docs check at
   implementation time.
4. Langfuse v4 sessions API shape (future export-training CLI) —
   untested; verify when that ticket lands (Spec correction #4).
