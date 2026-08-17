# Langfuse Deployment Guide

Self-host Langfuse to visualize agent traces, measure cache hit rate, tool
correctness rate, and analyze multi-agent trajectories.

## Prerequisites

- Docker + Docker Compose
- ModexAgent bot with `[observability]` extra installed:
  ```bash
  uv pip install -e ".[observability]"
  ```

## 1. Deploy Langfuse

The full stack (Langfuse v4 web + worker, ClickHouse, PostgreSQL, Redis, MinIO)
is defined in `docker-compose.langfuse.yml` at the repo root. All images use
the 轩辕 (xuanyuan) mirror with pinned versions.

Start Langfuse:

```bash
docker compose -f docker-compose.langfuse.yml up -d
```

Wait ~15s for ClickHouse migrations, then verify:

```bash
curl http://localhost:3000/api/public/health
# Expected: {"status":"OK","version":"4.0.0-rc.3"}
```

Open `http://localhost:3000` and create an account (first user is admin).

### Pinned image versions

| Component | Image | Version |
|-----------|-------|---------|
| Langfuse web | `docker.xuanyuan.run/langfuse/langfuse` | `4.0.0-rc.3` |
| Langfuse worker | `docker.xuanyuan.run/langfuse/langfuse-worker` | `4.0.0-rc.3` |
| ClickHouse | `docker.xuanyuan.run/clickhouse/clickhouse-server` | `25.12` |
| PostgreSQL | `docker.xuanyuan.run/postgres` | `16-alpine` |
| Redis | `docker.xuanyuan.run/redis` | `7` |
| MinIO | `docker.xuanyuan.run/minio/minio` | `RELEASE.2025-09-07T16-13-09Z` |

> Langfuse v4 requires ClickHouse ≥ 25.12. The infrastructure (ClickHouse /
> PostgreSQL / Redis) meets v4 requirements; only the Langfuse image tag needs
> updating when upgrading. See the [v3→v4 migration guide](https://langfuse.com/self-hosting/upgrade/upgrade-guides/upgrade-v3-to-v4)
> for production upgrades with data migration.

## 2. Configure ModexAgent

Edit `config/bot_config.yml`:

```yaml
observability:
  trace_backend: "otel_http"
  otel_endpoint: "${LANGFUSE_HOST:-http://localhost:3000}/api/public/otel/v1/traces"
  otel_service_name: "modex-bot"
  otel_headers:
    Authorization: "Basic ${LANGFUSE_BASIC_AUTH:-}"
    x-langfuse-ingestion-version: "4"
  prompt_capture: "summary"
```

Set credentials in `.env`:

```bash
LANGFUSE_HOST=http://localhost:3000
# base64(pk:sk) — generate with:
#   echo -n "pk-lf-xxx:sk-lf-xxx" | base64
LANGFUSE_BASIC_AUTH=<your-base64-auth-string>
```

Restart the bot. Traces now flow to Langfuse v4 via OTLP.

**Local `spans.jsonl` is still written** alongside OTLP export — the agent
self-read path (ADR-0024 D6) is preserved for future Phase 3 harness decisions.

## 3. Reading Trace Trees in Langfuse

Each agent turn produces a trace tree:

```
invoke_agent (root, full turn duration + stop_reason)
├── iteration.start (ReAct iteration boundary)
│   ├── chat (LLM call: api_duration_s, usage tokens, cache tokens, request model)
│   ├── execute_tool_batch (tool batch: tool_count, tool_names, end_time)
│   │   ├── execute_tool (per-tool: success, fail, error_type, result)
│   │   └── execute_tool (...)
│   └── iteration.end (iteration boundary + duration)
├── agent.handoff (multi-agent: target_agent, message_type, parent_turn_id)
│   └── [child agent's invoke_agent appears in the same trace via shared trace_id]
├── human.review (approval: decision, deny_reason, tool_name, tool_call_id)
└── training_tag (gen_ai.training.relevant flag)
```

**Key spans to look for:**

| Span | What it tells you |
|------|-------------------|
| `invoke_agent` | Turn-level duration, stop_reason (normal/max_iterations/error/cancelled) |
| `chat` | LLM call latency (`api_duration_s`), token usage, cache hit/miss, request model |
| `execute_tool` | Tool success/failure, error type, execution time |
| `iteration.start/end` | ReAct loop structure — how many iterations per turn |
| `agent.handoff` | Multi-agent dispatch — which agent was called, message type |
| `human.review` | Approval decisions — approved/denied, deny reason |

## 4. Key Metrics

### Cache Hit Rate

Per `chat` span, compare cache tokens to total input tokens:

```
cache_hit_rate = gen_ai.usage.cache_read_input_tokens / gen_ai.usage.input_tokens
```

High cache hit rate (>80%) means prompt caching is working. Low rate suggests
the system prompt is changing between calls (check for dynamic injection into
system prompt instead of user message).

### Tool Correctness Rate

Filter `execute_tool` spans by `gen_ai.tool.success`:

```
tool_success_rate = count(success=true) / total_execute_tool_spans
```

Drill into `gen_ai.tool.error_type` to find common failure patterns.

### Iteration Distribution

Count `iteration.start` spans per `invoke_agent` trace. A healthy agent
completes tasks in 3-8 iterations. P90 above 15 suggests the agent is looping
or stuck — investigate the `chat` spans to see what the LLM is doing.

### LLM Latency Breakdown

`chat` span's `api_duration_s` shows wall-clock LLM call time. Compare across
models (filter by `gen_ai.request.model`) to identify slow providers.

## 5. Eval Harness (Layer 1 + Layer 2)

The eval harness has three layers, each independently configurable:

### Layer 1: L2 Score Injection (production, in-process)

When `eval_score_injection: true` is set in `bot_config.yml`, the
`RootSpanHook` injects NUMERIC scores to Langfuse on the root
`invoke_agent` observation after each turn. 12 NUMERIC scores are injected per COMPLETED turn: tool_success_rate, tool_call_count, error_tool_count, iteration_count, llm_call_count, total_input_tokens, total_output_tokens, total_reasoning_tokens, api_latency_avg_s, cache_hit_rate, response_token_ratio, has_reasoning.

These appear automatically in Langfuse score analytics and dashboards.

### Layer 2: Dataset + Experiment Runner (offline, separate process)

Install the `[eval]` extra and use the CLI:

```bash
uv pip install -e ".[eval]"

# Curate a dataset from production traces (errors, high latency)
python -m bot.eval.cli curate --dataset react-baseline --max 50 --filter-errors

# Run an experiment against the dataset
python -m bot.eval.cli run \
  --dataset react-baseline \
  --experiment v1-prompt-test \
  --model openai/gpt-4o \
  --system-prompt "You are a helpful assistant."

# Compare experiment runs
python -m bot.eval.cli compare --dataset react-baseline
```

The experiment runner wraps `ReActAgent(mode="clean")` — no hooks,
governance, or approval. Each dataset item gets a fresh `AgentContext`
(zero state leakage). Three evaluators run per item: `accuracy` (substring
match against expected output), `completion` (boolean — completed without
error), `response_length` (character count). A run-level `avg_accuracy`
evaluator aggregates across items.

**Architecture note**: The eval CLI runs in a separate process to avoid
OTel tracer-provider conflicts with the bot's JSON-OTLP trace path. The
Langfuse SDK's auto-init would reuse the bot's global provider and add
its own span processor, causing duplicate traces for SDK-created spans.

## 6. LLM-as-a-Judge (Layer 3 Eval)

Layer 3 runs entirely on the Langfuse side — no code changes needed. It
uses an LLM to automatically score production traces.

### Prerequisites

1. **LLM Connection**: Navigate to Langfuse UI → Settings → LLM Connections.
   Add an OpenAI or Anthropic API key. The judge model must support
   structured output.

2. **Enabled trace ingestion**: Ensure `x-langfuse-ingestion-version: 4`
   header is set (already configured in `bot_config.yml` `otel_headers`).
   This enables real-time observation-level evaluation.

### Creating an Evaluator (UI)

1. Navigate to Langfuse UI → Evaluators → **+ Set up Evaluator**
2. Choose a **Managed Evaluator** (e.g., Helpfulness, Toxicity) or create
   a **Custom Evaluator** with your own rubric prompt
3. Select score type: **Numeric** (0-1 helpfulness), **Categorical**
   (correct/partial/incorrect), or **Boolean** (is_refusal, is_off_topic)
4. Choose the target data:
   - **Live Observations**: filter by `type=GENERATION`, `name=chat` to
     evaluate individual LLM calls
   - **Live Observations**: filter by `type=AGENT`, `name=invoke_agent` to
     evaluate the overall agent turn (trajectory-level)
5. Map variables: `{{input}}` → `observation.input`, `{{output}}` →
   `observation.output`
6. Set sampling rate (e.g., 10% to manage costs)

### Creating an Evaluator (API)

For reproducible deployments, use the `unstable-evaluators` API:

```bash
npx langfuse-cli api unstable-evaluators --help
npx langfuse-cli api unstable-evaluation-rules --help
```

> **Note**: The `unstable-evaluators` API is marked unstable and may
> change between Langfuse versions. UI configuration is more stable.

### What gets evaluated

| Target | Observation filter | What it captures |
|--------|-------------------|------------------|
| Individual LLM call | `type=GENERATION`, `name=chat` | Model response quality, hallucination, toxicity |
| Agent turn (trajectory) | `type=AGENT`, `name=invoke_agent` | Overall agent quality, task completion, reasoning |
| Tool call | `type=TOOL`, `name=execute_tool` | Tool selection correctness, argument quality |

### Cost management

- Use **sampling** (e.g., 5-10%) to limit evaluation costs
- Use **filters** to evaluate only specific sessions, tags, or user segments
- Typical cost: $0.01-0.10 per evaluation (depending on judge model)
- Pair with Layer 1 scores (free heuristics) to prioritize which traces
  get LLM-judge evaluation

## 7. Troubleshooting

**No traces appearing in Langfuse:**
- Verify `trace_backend: "otel_http"` and `otel_endpoint` are set
- Check `[observability]` extra is installed: `python -c "import opentelemetry.sdk.trace"`
- Check Langfuse is running: `curl http://localhost:3000/api/public/health`
- Check bot logs for OTLP export errors

**Traces appear but missing attributes:**
- `api_duration_s` missing: ensure `BeforeLLMHook` is registered (T9/T10)
- `gen_ai.request.messages` missing: ensure `prompt_capture: "summary"` is set
- `agent.handoff` missing: ensure `send_to_agent` was called (only fires on multi-agent dispatch)
- `human.review` missing: ensure approval is enabled and was triggered

**Local `spans.jsonl` still growing:**
- This is expected — dual-path (local + OTLP) is by design (ADR-0024 D6/D7)
- To stop local file writes: set `trace_backend: "otel_http"` without `file` (future `otel_only` tier, IN14)
