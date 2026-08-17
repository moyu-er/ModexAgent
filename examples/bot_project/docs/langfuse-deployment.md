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

When `OTEL_FORMAT=otel_http`, each completed turn emits a span tree to
Langfuse **and** writes local `spans.jsonl` (dual-path, ADR-0024 D6/D7).

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
**programmatic API** — no CLI command yet.

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

Aggregate the 12 trajectory metrics from local `spans.jsonl` — no Langfuse
or LLM needed:

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

**Local `spans.jsonl` still growing:**
Expected — dual-path (local JSONL + OTLP) is by design (ADR-0024 D6/D7).
The local copy powers `metrics` reports and `TrainingDataExporter` without
network round-trips.
