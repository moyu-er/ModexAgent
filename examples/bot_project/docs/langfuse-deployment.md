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

Create `docker-compose.langfuse.yml`:

```yaml
version: "3.8"
services:
  langfuse:
    image: langfuse/langfuse:latest
    ports:
      - "3000:3000"
    environment:
      DATABASE_URL: "postgresql://langfuse:langfuse@postgres:5432/langfuse"
      NEXTAUTH_SECRET: "change-me-to-a-random-string"
      SALT: "change-me-to-a-random-string"
      NEXTAUTH_URL: "http://localhost:3000"
      # Optional: disable signup for production
      DISABLE_SIGNUP: "false"
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: langfuse
      POSTGRES_PASSWORD: langfuse
      POSTGRES_DB: langfuse
    volumes:
      - langfuse_pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U langfuse"]
      interval: 5s
      timeout: 3s
      retries: 5

volumes:
  langfuse_pgdata:
```

Start Langfuse:

```bash
docker compose -f docker-compose.langfuse.yml up -d
```

Open `http://localhost:3000` and create an account (first user is admin).

## 2. Configure ModexAgent

Edit `config/bot_config.yml`:

```yaml
observability:
  trace_backend: "otel_http"
  otel_endpoint: "http://localhost:3000/api/public/otel"
  otel_service_name: "modex-bot"
  prompt_capture: "summary"
```

Restart the bot. Traces now flow to Langfuse via OTLP.

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

## 5. Flagged Trace → Dataset → Eval Workflow (Phase 3+ Preview)

1. **Flag** traces in Langfuse: mark traces where `stop_reason=max_iterations`,
   `stop_reason=error`, or iteration count > P90.
2. **Create dataset**: add flagged traces to a Langfuse dataset.
3. **Eval**: use DeepEval or Inspect AI to score trajectories from the dataset
   (Phase 3+ — not yet integrated).
4. **Improve**: use eval results to tune system prompts, tool descriptions, or
   harness parameters (max_iterations, error recovery strategy).

## 6. Troubleshooting

**No traces appearing in Langfuse:**
- Verify `trace_backend: "otel_http"` and `otel_endpoint` are set
- Check `[observability]` extra is installed: `python -c "import opentelemetry.sdk.trace"`
- Check Langfuse is running: `curl http://localhost:3000/api/public/otel`
- Check bot logs for OTLP export errors

**Traces appear but missing attributes:**
- `api_duration_s` missing: ensure `BeforeLLMHook` is registered (T9/T10)
- `gen_ai.request.messages` missing: ensure `prompt_capture: "summary"` is set
- `agent.handoff` missing: ensure `send_to_agent` was called (only fires on multi-agent dispatch)
- `human.review` missing: ensure approval is enabled and was triggered

**Local `spans.jsonl` still growing:**
- This is expected — dual-path (local + OTLP) is by design (ADR-0024 D6/D7)
- To stop local file writes: set `trace_backend: "otel_http"` without `file` (future `otel_only` tier, IN14)
