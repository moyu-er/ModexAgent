# Running Terminal Bench 2.1 (TB2.1)

How to run TB2.1 batches, resume them, re-run individual tasks, read the local
run data, and clean up tasks killed by environment failures. The launchers —
`run-tb21.ps1` (Windows) and `run-tb21.sh` (macOS/Linux) — wrap the real entry
point (`python -m bot.eval.harbor.tb21_batch`) with env hygiene, a warm-up
gate, and post-run cleanup. They are the only supported way to start a batch.

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
5. **A working proxy/VPN for containers**: trial containers download images,
   apt packages, pypi wheels, and task assets through the Docker proxy bridge.
   The launchers bypass the *host's* proxy only (host→langfuse/OTLP goes
   direct), so the proxy must be reachable from the Docker network. VPN nodes
   flap — the warm-up gate exists to catch this before money is spent.

## Quick start

```powershell
# Windows (pwsh) — run from anywhere; the script resolves the repo root
./run-tb21.ps1 -RunId tb21-all-v8
```

```bash
# macOS / Linux
chmod +x run-tb21.sh && ./run-tb21.sh tb21-all-v8
```

## Parameters

| Parameter | Default | Meaning |
| --- | --- | --- |
| `RunId` (positional on sh) | — required | Name of the whole test run. Defines the checkpoint/jobs/evidence namespace. |
| `Concurrency` | 8 | Parallel trials. 10 is the practical ceiling on Docker Desktop for Windows (network pool + CPU). |
| `TimeoutMultiplier` | 6 | Multiplier on the task's nominal install/build budget. The agent-phase wall clock is a flat 5400s per task (derived in `host_runtime.py`). |
| `PoolName` | `coder` | Which pool from `config/scopes/bot.yml` the eval agent is assembled from. |
| `Roster` | `benchmark` | Eval arm overlay (`config/scopes/eval/eval.yml`): benchmark persona, reduced tool surface, memory.core off. |
| `Tasks` | — | Comma-separated task subset (e.g. `"gpt2-codegolf,dna-assembly"`). Builds a micro-dataset of junction-linked task dirs so the batch sees only these tasks. **The checkpoint still gates**: a task with a checkpoint row is skipped even inside a micro-dataset — wipe its records first (see below). On sh it is the 6th positional arg. |

## Checkpoint semantics: resume, re-run, fresh

The batch reads `checkpoint.jsonl` at startup and `[skip]`s every task with a
row; one row is appended per finished task, pass or fail. Three workflows:

**Resume** — launch again with the same RunId; only unfinished tasks run.

**Re-run one task** — wipe the task's **three records** (job dir + evidence
JSON + checkpoint row — they must be wiped *together*; a task with evidence
but no checkpoint row also re-runs, overwriting its evidence), then relaunch
with `-Tasks`:

```bash
RUN=<repo>/.data/tb21-runs/tb21-all-v8
EV=<repo>/examples/bot_project/evals/evidence/tb21/tb21-all-v8
TASK=gpt2-codegolf
rm -rf "$RUN/jobs/$TASK"
rm -f  "$EV/$TASK.json"
grep -v "\"task\": \"$TASK\"" "$RUN/checkpoint.jsonl" > /tmp/ck && mv /tmp/ck "$RUN/checkpoint.jsonl"
rm -f  "$EV/report.json" "$EV/dashboard.html"   # aggregates regenerate per run
./run-tb21.sh tb21-all-v8            # same RunId — every other task is checkpoint-skipped
# or scoped: ./run-tb21.sh tb21-all-v8 8 6 coder benchmark "gpt2-codegolf"
```

```powershell
# Windows — -Tasks scopes the batch to the wiped task via a micro-dataset
./run-tb21.ps1 -RunId tb21-all-v8 -Tasks gpt2-codegolf
```

**Fresh full run** — a new RunId, or wipe the whole run directory + evidence
directory first.

## Where a run's data lives — and how to read it

| Path | Contents |
| --- | --- |
| `.data/tb21-runs/<RunId>/jobs/<task>/<task>__<id>/agent/` | `result.json` (stop_reason, spent_usd, error), `usage.json`, `install-result.json`, `pool-data/state.db`, `pool-data/runtime_state/**/spans.jsonl` |
| `.data/tb21-runs/<RunId>/jobs/<task>/<task>__<id>/verifier/` | `test-stdout.txt` (pytest output — failing assertions), `reward.txt` (final score), `ctrf.json` |
| `.data/tb21-runs/<RunId>/jobs/<task>/<task>__<id>/` | `trial.log`, `exception.txt` (harbor-level failures) |
| `.data/tb21-runs/<RunId>/batch.log` | Launcher + batch log: `[skip]`/`[run]`/`[done]` lines, warm-up probes, cleanup receipts |
| `.data/tb21-runs/<RunId>/checkpoint.jsonl` | The resume ledger — one JSON row per finished task |
| `evals/evidence/tb21/<RunId>/` | Per-task evidence JSON + `report.json` (solved/failed/accuracy) + `dashboard.html` (auto-refreshing) |
| Langfuse | Traces under experiment `terminalbench.tb21.<RunId>` |

**Score**: `report.json`. **One task's failure**: start at
`verifier/test-stdout.txt` (what the tests asserted), then
`agent/result.json` (how the agent phase ended).

**Reading the trajectory** (`state.db`, SQLite — the full message history):

```sql
-- message flow: roles, sizes, timing
SELECT id, role, token_count, created_at FROM memory_session_messages ORDER BY id;
-- assistant reasoning lives in message_json's top-level reasoning_content key
-- (NOT in the content column); tool calls live in message_json.tool_calls
SELECT message_json FROM memory_session_messages WHERE role='assistant' AND id=...;
-- compaction rows: role='compact'; pruned history: state IN ('soft_deleted','superseded')
SELECT id, state, COUNT(*) FROM memory_session_messages GROUP BY state;
```

**Reading the spans** (`spans.jsonl` — one JSON object per line; `chat` spans
carry the LLM-call truth): `gen_ai.request.max_tokens`,
`gen_ai.usage.input_tokens`, `gen_ai.response.finish_reasons`,
`gen_ai.system_instructions` (the exact prompt the model saw),
`gen_ai.input.messages`. The `invoke_agent` span's `end_time` marks turn
close — a `result.json` missing while `invoke_agent` closed long before the
wall clock is the signature of a post-turn hang.

## Cleaning up a poisoned task and re-running it

A task is **poisoned** when the environment — not the agent — killed it.
Confirm the poison signature in the task's data before wiping (see the
triage table below); a genuine capability failure re-runs the same way and
wastes the budget.

1. **Confirm the signature** — e.g. `verifier/test-stdout.txt` contains
   `tls handshake eof` / `Failed to fetch` (verifier dependency install died
   mid-outage), or `agent/install-result.json` shows
   `install_skipped: no_python_runtime` (trial env install died), or
   `agent/result.json` shows a hung-LLM cancellation with clean spans before
   it.
2. **Wipe the three records** + stale aggregates (snippet above).
3. **Relaunch** with `-Tasks <task>` (or the same RunId — checkpoint runs
   only the wiped task). The warm-up gate re-validates the network first; if
   it refuses, fix the proxy/VPN and relaunch — it has saved whole batches
   from dying at install.
4. **Done when**: `batch.log` shows `[run] <task>` then
   `[done] <task> -> <reward>` and the task's evidence JSON mtime is fresh.

Force-killed runs also leak `__env-*` containers and compose networks — the
launcher's pre-flight sweep removes them on next start, or sweep manually:
`docker ps -a --format '{{.Names}}' | grep __env- | xargs docker rm -f && docker network prune -f`.

## What the launcher does around the batch

1. **Env hygiene** — loads `.env`, strips `LLM_MODEL`/`LLM_API_KEY`/
   `MODEX_MAX_*` so `model.yml` is the only model-parameter source; clears
   host proxy vars (`NO_PROXY=*`) so host→langfuse/OTLP traffic goes direct
   while containers keep their own egress routing.
2. **Pre-flight sweep** — removes leaked `__env-*` trial containers and
   prunes networks (stale networks exhaust Docker Desktop's ~31-network
   address pool and kill every new trial).
3. **Warm-up gate (mandatory)** — probes container egress (LLM API, pypi,
   apt archive, uv installer) and pulls the apt index **serially** in one
   container before the batch. Background: a cold or flapping proxy pipe
   makes N concurrent `apt-get update`s time out and every trial dies at
   install (`no_python_runtime`) — the entire tb21-all-v7 first attempt
   failed this way in under 5 minutes, and repeated VPN-node flaps have been
   refused since. Any failed probe aborts the launch.
4. **The batch** — `tb21_batch` with `--delete` (trial containers removed
   per-task after verdict).
5. **Post-run cleanup (automatic)** — `docker network prune`, `docker volume
   prune`, stale `__env-*` container sweep. **Images are never deleted**:
   docker.io is unreachable from this region and the task-image set (~60 GB)
   must survive across runs; pulls go through the daemon registry-mirrors
   transparently. Receipts land in `batch.log`.
6. **Trajectories are kept** — `jobs/<task>/...` is the run evidence and is
   never touched by the launcher.

## Failure triage — the known failure classes

When a task fails, classify it before blaming the agent:

| Signature | Meaning | Treatment |
| --- | --- | --- |
| Verifier log: `tls handshake eof` / `Failed to fetch` / `Could not find a version` | Network outage during the verifier's dependency install — false negative | Poisoned: wipe + re-run |
| `install-result.json`: `install_skipped: no_python_runtime`, agent never started | Network outage during the trial environment install | Poisoned: wipe + re-run |
| `stop_reason=cancelled` with clean span pacing, error mentions dispatch watchdog | A hung LLM stream (single call never returned) | Poisoned: wipe + re-run |
| `stop_reason=timeout_kill`, artifacts missing, last spans far before the wall | Process hung after turn close (historically the hand-rolled completion tracker; now converged on session-tree quiesce) | Check `invoke_agent` span close time vs. wall; if work completed, wipe + re-run |
| `spent_usd` pinned at the budget cap | Budget-cap exhaustion while still working — a real (slow) failure | Keep; raise `DEFAULT_POOL_BUDGET_USD` only deliberately |
| `stop_reason=max_iterations` | The agent exhausted its iteration budget without finishing | Keep — real failure |
| Failing assertions with no network markers | Genuine capability/quality failure | Keep; read the trajectory for the why |

## Disk hygiene on the host (manual, when needed)

Task images are intentionally kept (~60 GB — see above). Deleting anything
else frees space *inside* the Docker VM; the host-side disk image (sparse
vhdx / qcow) never shrinks on its own. When the host disk fills:

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

Old runs accumulate under `.data/tb21-runs/` (spans.jsonl embeds full
message lists — a single 90-minute task can be ~0.5 GB). Keep the runs that
are your evidence baselines; delete intermediate/aborted runs whole.

## Where the knobs live

| What | Where |
| --- | --- |
| Model + params (max_output_tokens, reasoning_effort, temperature) | `examples/bot_project/config/model.yml` (threaded into trials via `MODEX_MAX_OUTPUT_TOKENS` in `bot/eval/harbor/model_source.py` → `entry.py`) |
| Per-task budget cap | `bot/eval/harbor/pool_budget.py` (`DEFAULT_POOL_BUDGET_USD`) |
| Agent wall clock | `bot/eval/harbor/host_runtime.py` (flat 5400s; multiplier from `task.toml`) |
| Bash tool timeout / description | `src/modex_agent/tools/terminal/persistent_bash.py` |
| Benchmark persona / tool surface | `config/scopes/eval/eval.yml` + `agents/benchmark.md` |
| Observability | `examples/bot_project/.env` (langfuse + OTLP) |
