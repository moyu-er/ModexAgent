# B5 memory-probe live gate

Run every command from `examples/bot_project`. The gate is manual and outside
CI because it uses a real answer model, the local Langfuse stack, and the OTel
collector.

## Prerequisites

Start the Langfuse stack and confirm both endpoints are reachable:

```powershell
docker compose -f docker-compose.langfuse.yml up -d
Invoke-RestMethod http://localhost:3000/api/public/health
Test-NetConnection localhost -Port 4318
```

Set the Langfuse and collector variables used by the harness:

```powershell
$env:LANGFUSE_HOST = "http://localhost:3000"
$env:LANGFUSE_PUBLIC_KEY = "pk-lf-..."
$env:LANGFUSE_SECRET_KEY = "sk-lf-..."
$env:LANGFUSE_BASIC_AUTH = "<base64 public-key:secret-key for OTLP>"
$env:OTEL_TRACES_ENDPOINT = "http://localhost:4318/v1/traces"
```

Set the answer model. `PROBE_RUN_*` takes precedence; when absent, the command
uses the corresponding `TEST_LLM_*` value.

```powershell
$env:PROBE_RUN_MODEL = "openai/step-3.7-flash"
$env:PROBE_RUN_API_KEY = "..."
$env:PROBE_RUN_BASE_URL = "https://provider.example/v1"
```

Optional controls are `PROBE_RUN_DATASET`,
`PROBE_RUN_MINIMUM_CALL_RESERVE_USD`, and
`PROBE_RUN_MAX_OUTPUT_TOKENS`. The B5 dispatch uses the deterministic T23
five-type scorer. The independent T24 memory judge and its `JUDGE_*` variables
are a separate judge/calibration pass and are not charged to this gate's cost
receipt.

## Seven-probe smoke dispatch

The committed `frozen_v1.jsonl` and `manifest_v1.json` are the smoke library:
five main probes plus two dual-arm controls. Use a unique run name and a cap of
at most one dollar:

```powershell
& ".venv\Scripts\python.exe" -m bot.eval.probes.run_cli `
  --library evals/probes/frozen_v1.jsonl `
  --manifest evals/probes/manifest_v1.json `
  --run-name "memory-probes.smoke-1" `
  --max-cost 1.00
```

The command fails before model dispatch if credentials, Langfuse health, or
collector connectivity are missing. It reserves budget for both no-memory
controls before starting the memory arm.

## Full 125+30 dispatch

Do not overwrite the committed smoke library. Generate the full frozen library
once into a separate directory; generation has its own cost cap and uses
`PROBE_GENERATOR_*` credentials:

```powershell
$env:PROBE_GENERATOR_MODEL = "openai/step-3.7-flash"
$env:PROBE_GENERATOR_API_KEY = "..."
$env:PROBE_GENERATOR_BASE_URL = "https://provider.example/v1"

& ".venv\Scripts\python.exe" -m bot.eval.probes.generate `
  --library-scale full `
  --seed 21 `
  --max-cost 2.00 `
  --output-dir evals/probes/full_v1
```

Proceed only when `evals/probes/full_v1/manifest_v1.json` reports
`status: complete`, 125 main probes, 30 dual-arm probes, and 155 total probes.
Then run the live gate with its independent five-dollar cap:

```powershell
& ".venv\Scripts\python.exe" -m bot.eval.probes.run_cli `
  --library evals/probes/full_v1/frozen_v1.jsonl `
  --manifest evals/probes/full_v1/manifest_v1.json `
  --run-name "memory-probes.full-1" `
  --max-cost 5.00
```

Reuse the exact run name and paths to resume a bounded partial harness run from
its checkpoint. Do not reuse a run name for a different library or model.

## Evidence acceptance

Both commands write `evals/evidence/b5_first_run.json`. Accept a complete run
only when all of the following hold:

- `schema_version` is `b5_first_run.v1` and `status` is `complete`.
- `preflight.langfuse_health` and `preflight.collector_port` are true, with an
  empty `preflight.missing` list.
- `type_scores.by_type` contains all five probe types.
- `dual_arm.completed_count` equals `dual_arm.expected_count`; the four count
  fields (`beneficial`, `harmful`, `ignored`, `neutral`) sum to that count.
- `experiment_compare_api_path` is `/api/public/experiments` and
  `experiment_compare` contains the exact run name.
- `failed_probe_count` is zero.
- `within_cost_cap` is true and `actual_cost_usd` does not exceed the command's
  `max_cost_usd`.

For the smoke library, expect 7 completed probes and 2 completed controls. For
the full library, expect 155 completed probes and 30 completed controls. A
`partial` status is a bounded diagnostic receipt, not a green gate; retain its
checkpoint and failures before deciding whether to resume.
