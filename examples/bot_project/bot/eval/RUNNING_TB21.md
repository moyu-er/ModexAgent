# Running Terminal Bench 2.1 (TB2.1)

This guide explains how to run TB2.1 evaluations against a bot_project agent:
full runs, resuming, and re-running individual tasks. Everything goes through
the cross-platform launchers in this directory — `run-tb21.ps1` (Windows) and
`run-tb21.sh` (macOS/Linux). They are thin wrappers around the real entry
point (`python -m bot.eval.harbor.tb21_batch`) plus mandatory post-run
resource cleanup.

## Prerequisites

1. **Docker Desktop running** (Linux: dockerd). Trial environments run in
   containers; the batch tears each one down with `--delete` after verdict.
2. **Dataset in place** at `<repo>/.data/terminal-bench-2-1/` — one directory
   per task (`task.toml` + `environment/` + `tests/`).
3. **`.env` configured** (in `examples/bot_project/`). It is the single source
   of truth for the observability stack: `LANGFUSE_HOST`,
   `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` (and `LANGFUSE_BASIC_AUTH` =
   base64 of `pk:sk`), `OTEL_TRACES_ENDPOINT`. The launcher loads it and
   strips conflicting model overrides so `config/model.yml` stays the single
   source for model parameters (max_output_tokens, reasoning_effort, ...).
4. **bot venv installed** (`examples/bot_project/.venv`) — the launcher uses
   `.venv/Scripts/python.exe` (Windows) / `.venv/bin/python` (POSIX).
5. **Network reachability from containers**: task images and runtime deps
   (pypi, github, apt) are downloaded inside trial containers. If they need
   a proxy/VPN, that proxy must be reachable from the Docker network — the
   launchers deliberately bypass the *host's* proxy only, so langfuse/OTLP
   traffic from the host goes direct.

## Quick start

```powershell
# Windows (pwsh)
cd examples/bot_project/bot/eval
./run-tb21.ps1 -RunId tb21-all-v7
```

```bash
# macOS / Linux
cd examples/bot_project/bot/eval
chmod +x run-tb21.sh
./run-tb21.sh tb21-all-v7
```

That runs the full 89-task dataset at concurrency 8. A run named
`tb21-all-v7` creates:

| Path | Contents |
| --- | --- |
| `<repo>/.data/tb21-runs/<RunId>/jobs/<task>/` | Per-task trial artifacts: `agent/result.json` (stop_reason, spent_usd), `agent/pool-data/state.db` (full message history incl. reasoning), `agent/pool-data/runtime_state/**/spans.jsonl` (trajectory spans), `verifier/` (test stdout, reward) |
| `<repo>/.data/tb21-runs/<RunId>/batch.log` | Batch + launcher log (skip/run/done lines, cleanup receipts) |
| `<repo>/.data/tb21-runs/<RunId>/checkpoint.jsonl` | One JSON row per finished task — the resume ledger |
| `examples/bot_project/evals/evidence/tb21/<RunId>/` | Per-task evidence JSON + `report.json` (solved/failed/accuracy) + `dashboard.html` |

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `RunId` (positional on sh) | — required | Name of the whole test run. Defines the checkpoint/jobs/evidence namespace. |
| `Concurrency` | 8 | Parallel trials. 10 is the practical ceiling on Docker Desktop for Windows (network pool + CPU). |
| `TimeoutMultiplier` | 6 | Multiplier on the task's nominal install/build budget (`task.toml` `[agent] timeout_sec` and `[tests]`). The agent-phase wall clock is a flat 5400s per task (multiplier derived in `host_runtime.py`). |
| `PoolName` | `coder` | Which pool from `config/scopes/bot.yml` the eval agent is assembled from. |
| `Roster` | `benchmark` | Eval arm overlay (`config/scopes/eval/eval.yml`): benchmark persona, reduced tool surface, memory.core off. |

## Checkpoint semantics: resume vs. re-run

The batch reads `checkpoint.jsonl` at startup; every task with a row is
`[skip]`ed. This gives three workflows for free:

**Resume a crashed/interrupted run** — just launch again with the same RunId:

```bash
./run-tb21.sh tb21-all-v7   # only unfinished tasks run
```

**Re-run one task** (e.g. after a fix, or to purge a network-poisoned
verdict) — wipe its three records, then relaunch with the same RunId:

```bash
RUN=<repo>/.data/tb21-runs/tb21-all-v7
TASK=gpt2-codegolf
rm -rf "$RUN/jobs/$TASK"                                   # trial artifacts
rm -f  examples/bot_project/evals/evidence/tb21/tb21-all-v7/$TASK.json   # evidence
grep -v "\"task\": \"$TASK\"" "$RUN/checkpoint.jsonl" > /tmp/ck && mv /tmp/ck "$RUN/checkpoint.jsonl"
./run-tb21.sh tb21-all-v7                                  # checkpoint runs only $TASK
```

(The three records must be wiped together — a task with evidence but no
checkpoint row would also re-run, overwriting its evidence.)

**Fresh full run** — use a new RunId, or wipe the whole run directory +
evidence directory first. Stale `report.json` / `dashboard.html` are
regenerated per run; remove them when you wipe tasks so aggregates stay
consistent.

## What the launcher does around the batch

1. **Env hygiene** — loads `.env`, strips `LLM_MODEL`/`LLM_API_KEY`/
   `MODEX_MAX_*` so `model.yml` is the only model-parameter source; clears
   host proxy vars (`NO_PROXY=*`) so host→langfuse/OTLP traffic goes direct
   while containers keep their own egress routing.
2. **Pre-flight sweep** — removes leaked `__env-*` trial containers and
   prunes networks (stale networks exhaust Docker Desktop's ~31-network
   address pool and kill every new trial).
3. **Warm-up gate (mandatory)** — before the batch starts, the launcher
   probes container egress (LLM API, pypi, apt archive, uv installer) and
   pulls the apt index **serially** in one task container. Background: a
   cold proxy pipe makes N concurrent `apt-get update`s time out and every
   trial dies at install (`no_python_runtime`) — the entire tb21-all-v7
   first attempt failed this way in under 5 minutes. If any probe fails,
   the launcher refuses to start (fix the proxy/VPN, re-run).
4. **The batch** — `tb21_batch` with `--delete` (trial containers are removed
   per-task after verdict).
4. **Post-run cleanup (automatic, always on)**:
   - `docker network prune`
   - **task-image sweep**: `alexgshaw/*` (original + mirror tags) and
     `terminal-bench/*` images — harbor `--delete` never removes images, and
     a full pull is ~60 GB. Zero residue after every run; images re-pull
     through the registry mirror on the next run (a few minutes' cold start).
   - `docker image prune -f` + `docker volume prune -f` (dangling layers and
     unreferenced volumes).
   - Receipts are appended to `batch.log`
     (`post-run cleanup: removed N task-image tags, ...`).
5. **Trajectories are kept** — `jobs/<task>/...` is the run evidence and is
   never touched by the launcher.

## Disk hygiene on the host (manual, when needed)

Deleting images frees space *inside* the Docker VM; the host-side disk image
(sparse vhdx / qcow) never shrinks on its own. When the host disk fills:

```powershell
# Windows (Docker Desktop), admin PowerShell — stop Docker first
Stop-Process -Name "Docker Desktop","com.docker.backend" -Force -ErrorAction SilentlyContinue
wsl --shutdown
Optimize-VHD -Path "<path-to>\DockerDesktopWSL\disk\docker_data.vhdx" -Mode Full
```

```bash
# Linux
sudo fstrim /var/lib/docker
# macOS: Docker Desktop > Settings > Resources > adjust disk image size,
# or Troubleshoot > Clean / Purge data.
```

## Reading results

- **Score**: `evals/evidence/tb21/<RunId>/report.json` (`solved`, `failed`,
  `accuracy`, per-task rows incl. `stop_reason`, `spent_usd`).
- **Live dashboard**: `evals/evidence/tb21/<RunId>/dashboard.html`
  (auto-refreshing).
- **A single task's failure**: `jobs/<task>/<trial>/verifier/test-stdout.txt`
  (failing assertions), `agent/result.json` (stop_reason: `completed` /
  `timeout_kill` / `max_iterations` / `error`, spent_usd), and the spans in
  `agent/pool-data/runtime_state/**/spans.jsonl` (chat calls, tool
  executions, token usage per iteration).
- **Langfuse**: traces land under the experiment
  `terminalbench.tb21.<RunId>` (host + trial containers report through the
  OTLP endpoint from `.env`).

## Failure triage — the known harness failure classes

When a task fails, classify it before blaming the agent:

| Signature | Meaning |
| --- | --- |
| `stop_reason=timeout_kill`, artifacts missing, last spans long before the wall | Hung turn-completion (historically the hand-rolled tracker, now converged on session-tree quiesce) or a hung LLM stream (dispatch watchdog). |
| Verifier log shows `tls handshake eof` / `Failed to fetch` / `Could not find a version` | Network outage during the verifier's dependency install — false negative; wipe + re-run the task. |
| `install-result.json: task_result=NO_TEST`, agent never started | Network outage during the trial environment install — same treatment. |
| `spent_usd` pinned at the budget cap | Budget-cap exhaustion while still working; the per-task budget is `DEFAULT_POOL_BUDGET_USD` (`bot/eval/harbor/pool_budget.py`). |
| `[hint: ... bash_input ...]` pollution in trajectories | (Historical) the silence-based stdin-wait false positive — removed; stdin-wait detection is now evidence-only (kernel probe / keyword / prompt shape). |

## Where the knobs live

| What | Where |
| --- | --- |
| Model + params (max_output_tokens, reasoning_effort, temperature) | `examples/bot_project/config/model.yml` (threaded into trials via `MODEX_MAX_OUTPUT_TOKENS` in `bot/eval/harbor/model_source.py` → `entry.py`) |
| Per-task budget cap | `bot/eval/harbor/pool_budget.py` (`DEFAULT_POOL_BUDGET_USD`) |
| Agent wall clock | `bot/eval/harbor/host_runtime.py` (flat 5400s; multiplier from `task.toml`) |
| Bash tool timeout / description | `src/modex_agent/tools/terminal/persistent_bash.py` |
| Benchmark persona / tool surface | `bot/eval/harbor/pool_mode_assembly.py` + `config/scopes/eval/eval.yml` |
| Observability | `examples/bot_project/.env` (langfuse + OTLP) |
