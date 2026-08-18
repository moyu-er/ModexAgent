# Langfuse Observability & Eval Usage Guide

Single usage entry point for **tracing** agent trajectories, **evaluating**
agent capability, and **collecting** training data from those trajectories.
Every capability below operates on the same unit — a trajectory (one agent
turn's span tree) — so the three concerns share one config surface and one
data store.

## Version Compatibility

The versions below are verified compatible. Upgrade any component by changing
the image tag in `docker-compose.langfuse.yml` and re-pulling — data volumes
survive `down` (only `down -v` deletes them).

| Component | Verified version | Source |
|-----------|-----------------|--------|
| Langfuse server (web + worker) | `4.11.0` | `docker-compose.langfuse.yml` |
| Langfuse Python SDK | `4.14.4` | `pyproject.toml` (`langfuse>=4.0.0`) |
| ClickHouse | `25.12` | `docker-compose.langfuse.yml` |
| PostgreSQL | `16-alpine` | `docker-compose.langfuse.yml` |
| Redis | `7` | `docker-compose.langfuse.yml` |
| MinIO | `RELEASE.2025-09-07T16-13-09Z` | `docker-compose.langfuse.yml` |
| OTel Collector (contrib) | `0.158.0` | `docker-compose.langfuse.yml` |
| LiteLLM | `1.90.1` | framework dependency |
| OpenTelemetry SDK + OTLP exporter | `1.44.0` | `[observability]` extra; range `>=1.33.1,<2` required by Langfuse SDK |

Langfuse v4 runs in **events_only mode** by default. This means writes
(ingestion, OTLP) work normally, but some v3 query endpoints are disabled —
the code uses v4 replacements (`v2/observations`, `v3/scores`,
`experiments`). See §9 for the consistency caveat this mode introduces.

### OpenTelemetry SDK — what it actually does

The `[observability]` extra installs `opentelemetry-sdk` +
`opentelemetry-exporter-otlp-proto-http` (both `1.44.0`, within Langfuse
SDK's required range `>=1.33.1,<2`). Three OTLP export paths coexist —
only one uses the OTel SDK directly:

| Path | Mechanism | Used by | Needs OTel SDK? |
|------|-----------|---------|:---:|
| **A. JSON OTLP** (main) | `_emit_span_via_json_otlp` — direct `httpx.Client.post` of hand-built OTLP JSON | All ReActAgent spans (invoke_agent, chat, tool, iteration, handoff, approval, training_tag) | No |
| **B. SDK Tracer** | `tracer.start_as_current_span` via `BatchSpanProcessor` → `OTLPSpanExporter` | `ExternalAgent` (Pi/OpenCode CLI harness) `invoke_agent` CLIENT span only | Yes |
| **C. Langfuse SDK OTel** | Langfuse SDK v4 auto-initializes OTel internally | `dataset.run_experiment()` traces in eval CLI | Yes (transitively) |

**Path A is the framework's primary span export.** `OtelSpanTraceStore.save_span`
builds OTLP JSON directly from `SpanModel` and POSTs via httpx — it bypasses
the OTel SDK's context-propagation model by design (the SDK generates its own
trace_id/span_id, which would lose our parent-child relationships).

**Path B is ExternalAgent-specific.** `external/agent.py:194` calls
`otel_trace.get_tracer("modex_agent.external")` to open a CLIENT span around
the external CLI turn. The tracer comes from `_build_otlp_tracer`
(`otel_store.py:279`), which builds a `TracerProvider` with a
`BatchSpanProcessor` and registers it globally via `set_tracer_provider`.
This is active when `config/pools/opencode/` uses `execution_strategy: external`.

**Path C is eval CLI-specific.** Langfuse Python SDK v4 auto-initializes
OTel on client construction; `dataset.run_experiment()` creates traces via
OTel spans internally. The SDK hard-depends on `opentelemetry-api/sdk
>=1.33.1,<2`.

**Do not uninstall the OTel SDK.** Path A would survive (it uses httpx), but
Path B silently degrades (`ImportError` → `yield None`, external agent
CLIENT spans lost) and Path C breaks (Langfuse SDK won't import).
`_require_observability_extra` also guards `trace_backend=otel_http` by
probing four OTel modules at startup — a missing extra produces a clear
`ImportError` with install instructions.

## 1. Deploy Langfuse

```bash
cd examples/bot_project
docker compose -f docker-compose.langfuse.yml up -d
```

Wait ~15s for ClickHouse migrations, then verify:

```bash
curl http://localhost:3000/api/public/health
# {"status":"OK","version":"4.11.0"}
```

Open `http://localhost:3000`, create the first user account (auto-admin),
then go to **Settings → API Keys** and create a key pair. You need both the
public key (`pk-lf-...`) and secret key (`sk-lf-...`) — they feed two
separate auth paths in §2.

### The `otel-collector` service (between the bot and Langfuse)

Since the 2026-08-17 collector migration, the compose stack includes an
OTel Collector service (`otel/opentelemetry-collector-contrib:0.158.0`,
pinned; 128M memory / 0.5 CPU limit; port bound to `127.0.0.1:4318`)
between the bot and Langfuse:

```
app (daemon sender thread) → collector :4318 (batch) → http://langfuse-web:3000/api/public/otel
```

With `OTEL_FORMAT=otel_http` (the default), `bot_config.yml` points
`otel_endpoint` at the collector
(`${OTEL_TRACES_ENDPOINT:-http://localhost:4318/v1/traces}`). The
collector is the reliability path: its `sending_queue` (4096) +
`retry_on_failure` (1s→5s backoff, 60s cap) buffer spans during a
Langfuse outage and redeliver when it returns.

Two deployment footguns (both verified on 0.158.0):

1. **The explicit `--config` flag is mandatory.** The image's default CMD
   loads `/etc/otelcol-contrib/config.yaml`, not the bind-mounted
   `/etc/otelcol/config.yaml`. Without
   `command: ["--config=/etc/otelcol/config.yaml"]` the collector
   silently runs its built-in default config — the receiver returns 200,
   the traces pipeline is absent, spans are dropped, and zero errors are
   logged.
2. **The exporter must be named `otlp_http/langfuse`.** `otlphttp` is a
   deprecated alias in 0.158.0 — the shipped `otel-collector.yaml` uses
   the modern name.

`otel-collector.yaml` sends `x-langfuse-ingestion-version: "4"` on every
export — keep this header in every deployment mode. Without it Langfuse
ingests via the slow path and v2 reads can lag by up to **15 minutes**;
with it, spans are queryable in seconds (~12 s via raw curl, ~2 s through
the collector's 1 s batch timeout). A `health_check` extension on
`:13133` reports readiness in `docker logs modex-otel-collector` (no
compose healthcheck — the distroless image has no shell).

**Memory budget (3072M total)**:

| Service | Limit |
|---|---|
| langfuse-web | 1024M |
| langfuse-worker | 768M |
| clickhouse | 768M |
| otel-collector | 128M |
| langfuse-db (postgres) | 128M |
| minio | 128M |
| redis | 128M |
| **Total** | **3072M** |

### Degradation behavior (R1–R6, drill-verified 2026-08-17)

The agent must run independently of the telemetry stack — degradation may
lose data, never block or crash a turn. R2/R3/R5/R6 semantics live in
`OtelSpanTraceStore` (daemon sender thread, 3 s per-POST timeout, drop
counters, bounded queue); R4 is the collector's `sending_queue` +
`retry_on_failure`.

| # | Scenario | Agent behavior | Data behavior |
|---|---|---|---|
| R1 | `OTEL_FORMAT=off` | normal | nothing emitted |
| R2 | collector refused | normal | dropped at sender + counted |
| R3 | collector hanging | normal | sender timeout 3 s, drop, keep draining |
| R4 | Langfuse down, collector up | normal | collector buffers, redelivers |
| R5 | long outage, queue full | normal, bounded memory | oldest dropped + counted |
| R6 | shutdown with queued spans | clean exit | best-effort flush ≤ 2 s |

Live drill evidence (2026-08-17, real harness turns; full ledger:
`.omo/notepads/otel-collector-migration/learnings.md`, "Ticket 10"):
baseline `otel_http` turn 3.013 s (6 spans exported, 0 dropped); R1
0.802 s (nothing emitted); R2 collector stopped → 3.057 s, +0.044 s vs
baseline, 1 span dropped as designed; R4 worker stopped → 1.843 s turn,
all 6 spans buffered and visible within 0.172 s of the post-restart
poll; rollback (`OTEL_FORMAT=file`) 2.170 s, 6-span `spans.jsonl` with
the complete legacy byte shape. R3/R5/R6 are covered by the resilience
test suite (`tests/unit/trace/test_otel_store_resilience.py`).

**Rollback** to pre-migration behavior: set `OTEL_FORMAT=file` and unset
`OTEL_TRACES_ENDPOINT` — the dormant FILE backend writes `spans.jsonl`
exactly as before; nothing else changes.

## 2. Configure `.env`

Four Langfuse variables + one LLM provider mapping. Copy `.env.example` to
`.env` and fill in:

```bash
# Langfuse OTLP export (bot runtime + eval harness)
OTEL_FORMAT=otel_http
LANGFUSE_HOST=http://localhost:3000
# base64(pk:sk) — generate with:
#   echo -n "pk-lf-xxx:sk-lf-xxx" | base64
LANGFUSE_BASIC_AUTH=<base64-of-your-pk-colon-sk>

# Langfuse SDK (eval CLI: run / compare / curate / setup-judge)
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...

# Trace segmentation (optional — all default to unset/default)
# LANGFUSE_ENVIRONMENT=production   # dev / staging / production — filter traces by deployment
# LANGFUSE_VERSION=1.2.0            # app or prompt version — for A/B testing and trace grouping
# LANGFUSE_TAGS=eval,math-qa        # comma-separated custom trace tags
```

`LANGFUSE_BASIC_AUTH` and `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` carry
the same key pair in two encodings — both are required because OTLP export
uses the Basic header while the Langfuse SDK uses separate pk/sk fields.

**LLM provider for eval CLI.** The `run` command constructs a
`LiteLLMProvider(model=...)` and LiteLLM reads provider keys from standard
env vars (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.), not from the bot's
`LLM_API_KEY`/`LLM_BASE_URL`. If your provider is behind a custom base URL
(StepFun, DeepSeek, GLM, etc.), map it:

```bash
# StepFun example — adapt the provider prefix for yours
export OPENAI_API_KEY="$LLM_API_KEY"
export OPENAI_API_BASE="$LLM_BASE_URL"
# then: --model openai/step-3.7-flash
```

The `metrics`, `replay-golden`, and `compare` commands do not need an LLM
key — they read local files or query Langfuse API only.

## 3. Trace (Bot Runtime)

Bot runtime observability is configured in `config/bot_config.yml` under
`observability:` — the `${ENV}` interpolation pulls from `.env`. Key fields:
`trace_backend` (file / otel_http / off), `prompt_capture` (off / hash /
summary / full), `trace_spans` (minimal / standard / full), `retain_reasoning_content`.

When `OTEL_FORMAT=otel_http` (the default), each completed turn's span
tree is exported through the collector to Langfuse — no local
`spans.jsonl` is written in this mode (`trace_backend: file` is the
dormant fallback, see §1; ADR-0024 D6 amendment).

### Span Tree

```
invoke_agent (root — turn duration, stop_reason, usage)
├── agent.start          (full tier — system prompt + tool defs)
├── iteration.start/end  (full tier — ReAct round boundaries)
├── chat                 (LLM call — model, usage tokens, cache, latency)
├── execute_tool_batch
│   └── execute_tool     (per-tool — success, error, result)
├── human.review         (approval — decision, deny reason)
├── agent.handoff        (subagent dispatch — child root parents here)
└── training_tag         (gen_ai.training.relevant flag)
```

### 12 Trajectory Metrics (auto-injected)

After each **COMPLETED** turn, `RootSpanHook` computes 12 metrics from the
turn's spans and POSTs them to Langfuse as NUMERIC scores (fire-and-forget,
failures are warning-only — a score-posting failure never breaks the turn):

| Metric | Direction | Source |
|--------|-----------|--------|
| `tool_success_rate` | high=good | non-error execute_tool / total |
| `tool_call_count` | neutral | execute_tool span count |
| `error_tool_count` | low=good | ERROR-status execute_tool count |
| `iteration_count` | low=good | iteration.start span count |
| `llm_call_count` | neutral | chat span count |
| `total_input_tokens` | high=cost | sum of chat span input_tokens |
| `total_output_tokens` | high=cost | sum of chat span output_tokens |
| `total_reasoning_tokens` | neutral | sum of reasoning.output_tokens |
| `api_latency_avg_s` | low=good | avg chat span wall-clock duration |
| `cache_hit_rate` | high=good | cache_read / input_tokens |
| `response_token_ratio` | neutral | output / (input + output) |
| `has_reasoning` | neutral | reasoning_tokens > 0 |

Token sums come from **chat spans only** — never the root span (cumulative
usage would double-count). Failed/cancelled turns get no capability scores;
their `stop_reason` is already in the root span attributes for the histogram.

### Training-Relevance Tagging

`TrainingDataHook` tags every turn with `gen_ai.training.relevant` (bool)
based on three rules: stop_reason must not be failure/cancel, iteration
count must be ≤ `training_max_iterations` (default 20), total tokens must be
≤ `training_max_tokens` (default 100K). `MAX_ITERATIONS` is intentionally
excluded from the failure list — a turn that ran the agent's own loop cap
can still be training-relevant. This tag gates SFT/DPO export (§6).

## 4. Eval (Offline CLI)

The eval CLI is a **separate process** — it never imports the bot runtime,
constructs its own agent via `bot.eval.agent_harness`. Observability is
env-driven (same `.env` vars), so eval traces and scores land in the same
Langfuse project as production traces.

```bash
cd examples/bot_project
set -a && . ./.env && set +a          # load .env into shell
# For `run` only — map LLM provider (see §2):
export OPENAI_API_KEY="$LLM_API_KEY"
export OPENAI_API_BASE="$LLM_BASE_URL"

python -m bot.eval.cli --help         # discover all commands
```

### Curate a Dataset

Collect production traces into a Langfuse dataset for reproducible eval:

```bash
python -m bot.eval.cli curate --dataset my-dataset --max 50
```

Pulls `invoke_agent` root observations from `v2/observations`, fetches each
trace's I/O, and creates dataset items. Items can be legacy (simple
`{"query": "..."}`) or v2 format (`EvalItemSpec` with `turns`, `toolset`,
`world_setup`, `world_assertions`).

### Run an Experiment

```bash
python -m bot.eval.cli run \
  --dataset my-dataset \
  --experiment baseline-v1 \
  --model openai/step-3.7-flash \
  --max-iterations 5 \
  --max-concurrency 2 \
  --mode clean \          # or production
  --toolset none           # none / read_only / read_write / full
```

Each dataset item gets a fresh `AgentContext` (zero state leakage). Five
evaluators run per item: `accuracy` (substring match), `completion`
(boolean — completed without error), `response_length` (char count),
`world_state` (boolean — all assertions passed), `tool_success` (NUMERIC —
span-derived success rate).

**Two modes:**

| Mode | Services | Use when |
|------|----------|----------|
| `clean` (default) | trace hooks only | Measuring prompt/model capability without runtime governance noise |
| `production` | trace + governance + loop detection + checkpoint | Measuring real-world behavior including governance effects |

Both modes inject 12 trajectory metrics when `OTEL_FORMAT=otel_http`.

**Legacy vs v2 items:** Only v2-format items (with `turns`/`toolset`/
`world_setup`) go through the full trace + score injection path. Legacy
items (simple `{"query": "..."}`) run without runtime services — they
produce evaluator scores but no trajectory metrics. Prefer v2 format for
new datasets.

### Compare Experiments

```bash
python -m bot.eval.cli compare --dataset my-dataset
```

Uses v4 `experiments` API + `v3/scores` time-window aggregation (the v3
`dataset-runs` endpoint is disabled in events_only mode). Output: per-run
average scores across all evaluators.

### Run Archives

Each `run` archives per-item outputs to
`evals/runs/{dataset}/{experiment}/{timestamp}.json` (gitignored). This is
your local evidence — Langfuse holds the scores and traces, the archive
holds the full output dicts.

## 5. Golden Cassette (Deterministic Replay)

Golden cases are recorded LLM transcripts that replay **offline, bit-identically**,
to detect agent behavior drift. No API keys needed for replay.

### Record

```bash
# Requires a real LLM (reads TEST_LLM_* env vars or OPENAI_API_KEY)
python -m bot.eval.cli record-golden --help   # see options
```

Records to `evals/golden/<case>/`: `item.json` (task spec), `meta.json`
(7-field fingerprint), `cassette/<trace_id>/` (content-addressed LLM call
payloads).

### Replay

```bash
python -m bot.eval.cli replay-golden --case evals/golden/<case>
```

**Four gates** — all must pass:

1. **Fingerprint match** — `meta.json` fields (model, temperature,
   `tool_names`, `tool_schema_sha256`, `prompt_sha256`, `platform`) must
   match the replay environment.
2. **Zero cassette misses** — every LLM call key found in the cassette.
3. **Clean turns** — every turn's `error` is None and `stop_reason` is
   `COMPLETED`.
4. **Non-vacuous oracle** — at least one `world_assertions` passes, OR
   `meta.json` carries `"baseline": true`.

**Platform pinning:** `meta.json`'s `platform` field gates OS-sensitive
cases. A golden recorded on `win32` will fail gate 1 on `darwin` — re-record
on the target platform. Shell-tool goldens are v2 (platform-pinned); v1
goldens deny shell tools via `deny_tools`.

### Cassette Contract

The harness wraps **only the LLM provider** — tools execute for real in
both record and replay, so world assertions check genuine side effects.
Workspace paths are normalized to the literal token `<workspace>` in tool
results so cassette keys stay stable across temp directories. See
`evals/README.md` for the full contract and `evals/DECISIONS.md` for the
flywheel decision log.

## 6. Training Data Export

`TrainingDataExporter` (`src/modex_agent/trace/training_exporter.py`)
derives SFT and DPO training datasets from traced trajectories. It is a
**programmatic API** today — no CLI command yet.

**Direction (shipped 2026-08-17)**: the active trace path is OTel-only —
app → OTel Collector (contrib 0.158.0, retry/buffer) → Langfuse, which is
the system of record. Local `spans.jsonl` stays as a **dormant legacy mode**
(`trace_backend: file`, selectable fallback — not deleted). The exporter
reads via `LangfuseTraceQuery` (Langfuse `v2` API) in otel_http mode, with
read-side reverse-normalization (TOOL observations restored to
`execute_tool`, metadata attributes authoritative, `{"result": ...}`
envelopes unwrapped). Subagent notifications no longer carry a `Trace:`
path. Design + drill evidence: `docs/design/otel-collector/PRD.md`.

**Still planned (not yet implemented)**:

- `export-training` CLI command (`bot.eval.cli`) — session auto-discovery
  via Langfuse `v2/sessions` + the exporter.
- Retention via Langfuse/ClickHouse TTL (replaces local-file retention).

### SFT Export

```python
from pathlib import Path
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.training_exporter import TrainingDataExporter

store = OtelSpanTraceStore(Path(".modex/runtime_state/default/trace"))
exporter = TrainingDataExporter(store, output_dir=Path("training_data"))

result = await exporter.export_sft(
    session_ids=["conv123.main", "conv456.coder"],  # required — no enumeration
    # since=1719000000.0,  # optional time bounds
    # until=1719999999.0,
)
# → training_data/sft_<timestamp>.jsonl (OpenAI messages format)
```

Filters: only `gen_ai.training.relevant=true` trajectories (§3), ≥2 messages
(user + assistant). 3-tier dedup: exact SHA-256 → n-gram Jaccard (≥0.8) →
semantic (not implemented, Phase 1).

### DPO Export

```python
result = await exporter.export_dpo(
    session_ids=["conv123.main", "conv456.coder"],
)
# → training_data/dpo_<timestamp>.jsonl
```

Pairs **approved** (chosen) and **denied** (rejected) trajectories for the
same task (matched by user message). Requires `human.review` spans with
approval decisions. Filters: score gap ≥0.5, edit-distance ratio ≥0.1,
refusal filtering. Dedup by exact hash of (prompt, chosen, rejected).

### Cross-Tenant Guard

Both exports warn if trajectories span multiple tenants (scope-aware
filtering on session IDs). Pass `allow_cross_tenant=True` to suppress when
intentional.

## 7. Local Metrics Report

Aggregate the 12 trajectory metrics from local `spans.jsonl` (legacy /
FILE-mode data — `otel_http` writes no local jsonl; for live data use the
Langfuse UI §8 or `compare`) — no Langfuse or LLM needed:

```bash
python -m bot.eval.cli metrics --workspace . --days 7
```

Reads `.modex/runtime_state/*/trace/*/spans.jsonl`, computes per-subtree L2
averages for COMPLETED root traces, and reports: stop_reason histogram,
approval decisions, handoff counts, cleanup metrics (tokens_saved,
savings_rate, thrash), and the 12 metric averages. Use this for quick
capability trend checks without touching Langfuse.

## 8. Reading Traces in Langfuse UI

Open `http://localhost:3000/traces`. Each agent turn is one trace with the
span tree from §3. Filter and sort by:

- **`tool_success_rate`** (score) — find worst trajectories
- **`api_latency_avg_s`** (score) — find slow LLM calls
- **`cache_hit_rate`** (score) — monitor prompt caching effectiveness
- **`stop_reason`** (root span attribute) — filter `error` / `cancelled` /
  `max_iterations`

Drill into a trace to see the span tree: `chat` spans show token usage and
latency; `execute_tool` spans show success/failure and error type;
`iteration.start/end` shows ReAct loop structure.

For programmatic trace/score queries, use the `langfuse` skill (loads
`langfuse-cli` via npx) — it wraps the v4 API endpoints with correct auth.

## 9. Troubleshooting

**Scores show `(unavailable)` in `compare`:**
The experiment's time window had no scores. Either `OTEL_FORMAT` wasn't
`otel_http` when the experiment ran, or the v4 ClickHouse consistency delay
(see below) hasn't settled. Re-run `compare` after a few seconds.

**v4 ClickHouse consistency delay:**
Langfuse v4 events_only mode ingests asynchronously. The ingestion API
returns `207` (success) immediately, but scores may take seconds to appear
in `v3/scores` queries, and some scores in a batch may be temporarily
invisible while others are visible. This is a known v4 characteristic, not
a code bug. If scores don't appear after ~30s, check the worker logs:
`docker compose -f docker-compose.langfuse.yml logs langfuse-worker`.

**No traces in Langfuse UI:**
1. Confirm `OTEL_FORMAT=otel_http` (not `file` / `off`)
2. Confirm health: `curl http://localhost:3000/api/public/health`
3. Confirm `[observability]` extra: `python -c "import opentelemetry.sdk.trace"`
4. Check `LANGFUSE_BASIC_AUTH` is non-empty and matches your pk:sk

**`replay-golden` fails fingerprint gate:**
Expected when platform or tool schema changed. Check the diff output —
`platform: recorded='win32', constructed='darwin'` means re-record on the
target OS. `tool_schema_sha256` mismatch means a tool's schema changed —
re-record the golden.

**`run` exits with "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY required":**
The eval CLI needs SDK keys (separate from `LANGFUSE_BASIC_AUTH`). See §2.

**`run` fails with LLM auth error:**
The eval CLI uses `LiteLLMProvider`, which reads standard provider env vars
(`OPENAI_API_KEY` etc.), not the bot's `LLM_API_KEY`. See §2 for the
mapping.

**Local `spans.jsonl` not growing under `otel_http`:**
Expected — since the collector migration, `otel_http` is OTel-only (no
local jsonl dual-write). Only `trace_backend: file` writes `spans.jsonl`;
the `metrics` CLI reads those legacy/FILE files.

**Traces appear in Langfuse only after ~15 minutes:**
The `x-langfuse-ingestion-version: "4"` header is missing from the export
path. Without it Langfuse ingests via the slow path — v2 reads lag up to
15 minutes. The shipped `otel-collector.yaml` sends the header; keep it
in every deployment mode.

**`/api/public/v2/traces*` returns 404:**
Removed in v4 events_only mode. Read `v2/observations` instead and group
by `traceId` when you need trace-level views.

## 10. Operations: Data Retention & Disk Growth

### What Langfuse manages natively vs what we own

Langfuse ships a built-in **Data Retention** feature: a nightly job deletes
traces, observations, scores, and media assets older than N days, configured
per project in Project Settings or via the org-scoped Projects API. **It is an
Enterprise Edition feature** — on self-hosted OSS without
`LANGFUSE_EE_LICENSE_KEY` the setting is unavailable (the UI hides it, and
`PUT /api/public/projects/{id}` returns 403 "Organization-scoped API key
required"; org keys and the Instance Management API are EE-gated too).

Langfuse's own docs sanction the OSS fallback
([scaling → Increasing Disk Usage](https://langfuse.com/self-hosting/configuration/scaling)
→ ClickHouse Disk Usage): **ClickHouse TTL on the trace tables + S3 lifecycle
rules for the event blob prefix**. That is what this deployment uses. We own
these two knobs; Langfuse will not fight them (its migrations only
`ADD COLUMN`-style alter, never touching TTL).

### Configured policy (applied 2026-08-18, all live-verified)

| Store | Mechanism | Retention |
|-------|-----------|-----------|
| ClickHouse `traces`, `scores` | `MODIFY TTL timestamp + INTERVAL 180 DAY` | 180 days |
| ClickHouse `observations`, `events_core`, `events_full` | `MODIFY TTL start_time + INTERVAL 180 DAY` | 180 days |
| ClickHouse `blob_storage_file_log` | `MODIFY TTL created_at + INTERVAL 180 DAY` | 180 days |
| MinIO bucket `langfuse`, prefix `events/` | ILM expiry rule | 180 days |
| MinIO prefix `media/` | none — Langfuse docs: media lifecycle rules break trace references | indefinite |
| ClickHouse system log tables | config.d opt-out in `docker-compose.langfuse.yml` | write-disabled |

`blob_storage_file_log` rows track the `events/` blobs one-to-one, so the
table's TTL column is `created_at` aligned with the MinIO ILM window — rows
expire as their blobs do, and the table cannot grow unbounded the way the
system log tables did. (Langfuse's retention FAQ explicitly treats a TTL on
this table as a supported mechanism.)

Evidence: `SHOW CREATE TABLE` shows the TTL clause on all six tables and it
survives container recreation (TTL lives in the data volume's table metadata,
not the container). Expired data drops organically during background TTL
merges (`ttl_merge_frequency`, default 4h) — the stale 2025-08 partition in
`events_full` was dropped this way immediately after the TTL was applied.
Ingest was re-verified end-to-end after all changes: synthetic span →
collector `:4318` → `events_full` row within 5s → visible in
`GET /api/public/v2/observations`.

### Automated provisioning (`langfuse-retention-init`)

Everything in the table above is applied by the one-shot
`langfuse-retention-init` compose service on every `docker compose up -d`
(fresh volume included) — no manual ALTERs, nothing to forget. It runs the
minio image (ships `mc` + `curl`), waits for `langfuse-web` health and for
Langfuse's migrations to create the six tables, then converges:

1. ClickHouse `MODIFY TTL` per table — skipped when the target TTL is
   already present (checked via `SHOW CREATE TABLE`).
2. MinIO `mc ilm rule add --prefix events/` — only when no `events/` rule
   exists (`mc ilm rule add` would otherwise create duplicates; verified
   live).

Then it exits (`restart: "no"`). The whole run is idempotent — re-running
produces only `ok/skip` log lines, zero new mutations, zero duplicate rules.

**Memory budget:** the service has a 64M cap but only during the boot window
— it exits immediately after converging, so the steady-state budget stays
3072M across the 7 long-running services. (Shrinking MinIO to fund it
permanently was rejected: T13 showed MinIO OOM-kills at 128M under span
bursts; halving it would reopen a known failure mode for a one-shot's sake.)

**Troubleshooting:**

```bash
# init logs (the authoritative record of what was applied/skipped):
docker compose -f docker-compose.langfuse.yml logs langfuse-retention-init
# exit state (expect: Exited (0), oom=false):
docker inspect modex-langfuse-retention-init --format "{{.State.Status}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}}"
# re-run (e.g. after fixing a problem, or to converge after edits):
docker compose -f docker-compose.langfuse.yml up -d langfuse-retention-init
```

The service waits up to 240s for web health and 300s per table — hard upper
bounds: the deadline is checked before each poll, every poll's curl is capped
at `min(10s, remaining budget)` (hanging polls cannot extend the window), and
the inter-poll sleep is capped at the remaining budget too (residual
overshoot: the 1s granularity of `date +%s`). On timeout it exits non-zero
with a `FATAL:` line naming what it
waited for. An `ALTER` that exceeds the 10s per-call cap exits non-zero too,
but the mutation keeps running server-side — re-run the service and it will
converge (skip what landed). It does not
depend on `langfuse-web: service_healthy` because web defines no healthcheck
(and adding one means touching the core service) — the script polls
`/api/public/health` itself, which is also the signal that migrations have
started.

### How to change the retention window

1. Edit `RETENTION_DAYS` in the `langfuse-retention-init` service block of
   `docker-compose.langfuse.yml`, then re-run it — ClickHouse TTLs converge
   automatically:

```bash
docker compose -f docker-compose.langfuse.yml up -d langfuse-retention-init
```

2. The MinIO rule cannot be day-edited blindly (`mc ilm rule edit` needs the
   rule ID): the init log prints a WARN with the exact command when the
   `events/` rule days disagree with `RETENTION_DAYS`. Find the ID and edit:

```bash
docker exec modex-langfuse-minio mc ilm ls local/langfuse
docker exec modex-langfuse-minio mc ilm rule edit local/langfuse --id <id> --expire-days 90
```

Equivalent manual ClickHouse commands (the init script's own statements, for
reference or one-off use without the service):

```bash
docker exec modex-langfuse-clickhouse clickhouse-client \
  --query "ALTER TABLE default.traces MODIFY TTL timestamp + INTERVAL 90 DAY"
docker exec modex-langfuse-clickhouse clickhouse-client \
  --query "ALTER TABLE default.observations MODIFY TTL start_time + INTERVAL 90 DAY"
docker exec modex-langfuse-clickhouse clickhouse-client \
  --query "ALTER TABLE default.scores MODIFY TTL timestamp + INTERVAL 90 DAY"
docker exec modex-langfuse-clickhouse clickhouse-client \
  --query "ALTER TABLE default.blob_storage_file_log MODIFY TTL created_at + INTERVAL 90 DAY"
# events tables need the full-text-index flag (their text indices re-validate on ALTER):
docker exec modex-langfuse-clickhouse clickhouse-client --enable_full_text_index=1 \
  --query "ALTER TABLE default.events_full MODIFY TTL start_time + INTERVAL 90 DAY"
docker exec modex-langfuse-clickhouse clickhouse-client --enable_full_text_index=1 \
  --query "ALTER TABLE default.events_core MODIFY TTL start_time + INTERVAL 90 DAY"
```

**Ordering footgun (hit live on 2026-08-18):** applying TTL to the events
tables spawns a `MATERIALIZE TTL` mutation; when the ClickHouse server idles
near its 768M cap it dies with `MEMORY_LIMIT_EXCEEDED` and the stuck mutation
blocks all later ALTERs on that table. If it happens:
`KILL MUTATION WHERE database='default' AND table='events_full'` (and
`events_core`), then retry — with the system-log opt-out active the server
idles ~500MiB and the mutation completes.

**Fresh-volume rebuild:** nothing manual. After `down -v` + `up -d`,
Langfuse migrations recreate the tables without TTL, and
`langfuse-retention-init` — which `up -d` always runs — waits for those
tables, then applies the full policy (TTL + ILM) automatically. Its
container appearing as `Exited (0)` in `docker compose ps -a` is the success
state, not a fault.

### Disk-growth expectations

- **Langfuse trace data is small**: `events_full` 14.8 MiB + `events_core`
  2.9 MiB + `scores` 12 KiB after ~2 weeks of runs (~8.7k events). At this
  rate expect single-digit MiB/day; the 180d TTL caps the working set at a few
  GiB worst case. Blob (`events/`) objects add roughly the same order (one
  JSON per event, ~0.6-2 KiB each; the ILM rule caps them at 180d too).
- **The historic growth driver was ClickHouse system log tables**, not
  Langfuse data: `trace_log` 1.40 GiB / 70M rows (query profiler writes
  continuously), `asynchronous_metric_log` 405 MiB / 688M rows,
  `text_log` 499 MiB, `part_log` 194 MiB, `metric_log` 190 MiB — vs ~18 MiB
  of actual trace data. These are now write-disabled via
  `docker-compose.langfuse.yml` (`<trace_log remove="1"/>` etc., the Langfuse
  Terraform-module default). `query_log`, `part_log`, `error_log` stay on
  per Langfuse docs (useful, small).
- **Existing system-table data is not reclaimed** by the opt-out — the ~2.7
  GiB already written stays on disk. One-time manual reclamation if disk
  pressure demands (owner decision, destructive, skipped by the 2026-08-18
  change):
  `docker exec modex-langfuse-clickhouse clickhouse-client --query "SET max_table_size_to_drop=0; TRUNCATE TABLE system.trace_log"` (repeat per table).

### ClickHouse memory cap & transient errors (T13)

ClickHouse is capped at 768M (settled 3GB-stack budget). Known behaviors
under large scans/bursts, all observed in T13 drills and again during this
change:

- Fat `events_full` scans under the cap yield **transient 422s AND HTTP-200
  empty pages** — always poll queries to count-convergence, never conclude
  from a single empty read.
- Unthrottled ~1.5k-span bursts OOM-kill MinIO and the collector/worker;
  ~7 spans/s is the safe ingest rate.
- TTL-materialization mutations count as big scans: they OOM when the server
  idles near the cap (see the KILL MUTATION runbook above). Removing the
  system log tables dropped idle RSS from ~767MiB to ~500MiB, restoring
  headroom for merges.

### Monitoring: table sizes

```bash
docker exec modex-langfuse-clickhouse clickhouse-client --query "
SELECT table, formatReadableSize(sum(bytes_on_disk)) AS disk, sum(rows) AS rows
FROM system.parts WHERE active AND database='default'
GROUP BY table ORDER BY sum(bytes_on_disk) DESC FORMAT PrettyCompact"

# system-table check (frozen after the opt-out; query_log may grow slowly):
docker exec modex-langfuse-clickhouse clickhouse-client --query "
SELECT name, total_rows, formatReadableSize(total_bytes) FROM system.tables
WHERE database='system' AND engine LIKE '%MergeTree%' AND total_bytes > 0
ORDER BY total_bytes DESC LIMIT 10 FORMAT PrettyCompact"
```

Watch that `events_full` disk trends with ingest rate and that no
`system.*_log` row count moves except `query_log`.

