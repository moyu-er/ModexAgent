#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run-tb21.sh — TB2.1 batch launcher (macOS/Linux) with automatic post-run
# cleanup. POSIX-bash twin of run-tb21.ps1.
#
# Usage:
#   ./run-tb21.sh <run-id> [concurrency] [timeout-multiplier] [pool] [roster] [tasks]
#
#   run-id            the name of the whole test run (e.g. "tb21-all-v7").
#                     Selects the checkpoint/jobs/evidence namespace: tasks
#                     already recorded in the run's checkpoint are skipped
#                     automatically; wiping a task's job dir + evidence +
#                     checkpoint row forces that single task to re-run.
#   concurrency       default 8
#   timeout-multiplier default 6 (install/build budget multiplier)
#   pool              default coder
#   roster            default benchmark
#   tasks             optional comma-separated task subset (e.g.
#                     "fix-git,torch-pipeline-parallelism") — symlinked into a
#                     micro-dataset so the batch sees only these tasks
#
# Post-run cleanup (always on, results logged to batch.log):
#   1. docker network prune   — trial networks the --delete teardown missed
#   2. docker volume prune -f — unreferenced task volumes
#   3. stale __env-* container sweep
#   IMAGES ARE NEVER DELETED: the task-image set is expensive and must
#   survive across runs. Trial image pulls go through registry mirrors
#   configured at the daemon level.
# Trajectories (jobs/<task>/...) are always kept — they are the run evidence.
#
# NOT automated (needs root; the Docker VM disk is sparse and never shrinks
# on its own — run manually when the host disk fills):
#   Linux:  sudo fstrim /var/lib/docker            # ext4/xfs
#   macOS:  Docker Desktop > disk image size settings, or
#           cd ~/.docker/... && docker run --rm --privileged ... podman system reset
#           (simplest: Docker Desktop > Troubleshoot > Clean / Purge data)
# ---------------------------------------------------------------------------
set -euo pipefail

RUN_ID="${1:?usage: run-tb21.sh <run-id> [concurrency] [timeout-multiplier] [pool] [roster] [tasks]}"
CONCURRENCY="${2:-8}"
TIMEOUT_MULT="${3:-6}"
POOL_NAME="${4:-coder}"
ROSTER="${5:-benchmark}"
TASKS="${6:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# <repo>/examples/bot_project/bot/eval/run-tb21.sh -> repo root is 4 levels up
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
BOT_DIR="$REPO_ROOT/examples/bot_project"
DATASET="$REPO_ROOT/.data/terminal-bench-2-1"
LOG_DIR="$REPO_ROOT/.data/tb21-runs/$RUN_ID"

if [ ! -d "$BOT_DIR/.venv" ]; then
    echo "bot venv not found at $BOT_DIR/.venv — run install first" >&2
    exit 1
fi
if [ ! -d "$DATASET" ]; then
    echo "dataset not found at $DATASET" >&2
    exit 1
fi

cd "$BOT_DIR"
mkdir -p "$LOG_DIR"

log() { echo "$1" >> "$LOG_DIR/batch.log"; }

# --- Optional task subset (arg 6, "a,b"): symlink a micro-dataset so the
# --- batch sees only the named tasks (fresh run-id => no checkpoint rows).
if [ -n "$TASKS" ]; then
    MICRO="$REPO_ROOT/.data/tb21-runs/$RUN_ID-dataset"
    rm -rf "$MICRO"
    mkdir -p "$MICRO"
    IFS=',' read -ra _TASK_LIST <<< "$TASKS"
    for T in "${_TASK_LIST[@]}"; do
        T="$(echo "$T" | xargs)"
        [ -z "$T" ] && continue
        SRC="$DATASET/$T"
        if [ ! -f "$SRC/task.toml" ]; then
            echo "unknown task '$T' (no task.toml under $SRC)" >&2
            exit 1
        fi
        ln -s "$SRC" "$MICRO/$T"
    done
    DATASET="$MICRO"
    log "micro-dataset for run $RUN_ID : $TASKS"
fi

# --- env: .env is the source of truth (langfuse/otel endpoints + auth);
# --- strip stale model overrides so model.yml stays the single source.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi
unset LLM_MODEL LLM_API_KEY LLM_BASE_URL MODEX_MAX_CONTEXT_TOKENS MODEX_MAX_OUTPUT_TOKENS
export MODEX_POOL_NAME="$POOL_NAME"
export MODEX_EVAL_ROSTER="$ROSTER"
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1

# --- The host-side process must bypass any configured proxy so langfuse/
# --- otel traffic goes direct; trial containers keep their own routing.
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY="*"
export no_proxy="*"

# --- Pre-flight sweep: force-killed runs leak trial containers + networks.
STALE="$(docker ps -a --format '{{.Names}}' 2>/dev/null | grep '__env-' || true)"
if [ -n "$STALE" ]; then
    echo "$STALE" | xargs -r docker rm -f >/dev/null 2>&1 || true
    log "swept $(echo "$STALE" | wc -l | tr -d ' ') stale trial containers"
fi
docker network prune -f >/dev/null 2>&1 || true

# --- Warm-up (mandatory, before the batch): the trial agent install path
# --- needs apt/pypi/github through the Docker proxy. A cold or flapping
# --- proxy makes N concurrent `apt-get update`s time out and every task
# --- dies at install ("no_python_runtime") — observed on tb21-all-v7
# --- (2026-08-25). Each step retries up to 3 times (VPN nodes flap
# --- transiently); all attempts failing means the network is truly down.
WARMUP_TRIES=3

probe_egress() {
    docker run --rm curlimages/curl:latest sh -c '
for t in "https://api.deepseek.com/v1/models|llm" "https://pypi.org/simple/|pypi" "http://archive.ubuntu.com/ubuntu/dists/noble/InRelease|apt" "https://astral.sh|uv"; do
    url="${t%%|*}"; name="${t##*|}"
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$url")
    echo "$name=$code"
done' 2>/dev/null || true
}

PROBE_OK=0
TRY=1
while [ "$TRY" -le "$WARMUP_TRIES" ]; do
    log "warm-up: probing container egress (attempt $TRY/$WARMUP_TRIES)"
    PROBE="$(probe_egress)"
    echo "$PROBE" | while IFS= read -r line; do
        if [ -n "$line" ]; then
            log "warm-up probe: $line"
        fi
    done
    BAD="$(echo "$PROBE" | grep -Ev '^(llm|pypi|apt|uv)=(200|401)$' | grep -E '=' || true)"
    COUNT="$(echo "$PROBE" | grep -cE '^(llm|pypi|apt|uv)=[0-9]+$' || true)"
    if [ -z "$BAD" ] && [ "${COUNT:-0}" -eq 4 ]; then
        PROBE_OK=1
        break
    fi
    log "warm-up probe attempt $TRY failed: $BAD"
    TRY=$((TRY + 1))
done
if [ "$PROBE_OK" -ne 1 ]; then
    log "warm-up FAILED: container egress not ready after $WARMUP_TRIES attempts. Refusing to start."
    echo "warm-up failed: container egress not ready after $WARMUP_TRIES attempts. Fix the proxy/VPN, then re-run." >&2
    exit 1
fi

# apt index warm-up: pull the apt indexes through the proxy once, serially,
# so the batch's concurrent installs don't cold-start it. Uses the FIRST
# TASK'S OWN IMAGE (the trial's ubuntu base) — no hardcoded helper image;
# with a -Tasks subset it is exactly the image the rerun will use.
WARM_TASK="$(echo "$TASKS" | tr ',' '\n' | awk 'NF{print $1; exit}')"
if [ -z "$WARM_TASK" ]; then
    WARM_TASK="$(ls -1 "$DATASET" | head -n 1)"
fi
WARM_REF="alexgshaw/${WARM_TASK}:20251031"
log "warm-up: apt index pull (serial, image $WARM_REF)"
APT_OK=0
TRY=1
while [ "$TRY" -le "$WARMUP_TRIES" ]; do
    APT_WARM="$(docker run --rm "$WARM_REF" \
        sh -c 'apt-get update -qq >/dev/null 2>&1; echo "apt_update_exit=$?"' 2>/dev/null || true)"
    log "warm-up apt attempt $TRY: $APT_WARM"
    if [ "$APT_WARM" = "apt_update_exit=0" ]; then
        APT_OK=1
        break
    fi
    TRY=$((TRY + 1))
done
if [ "$APT_OK" -ne 1 ]; then
    log "warm-up FAILED: apt-get update did not succeed after $WARMUP_TRIES attempts. Refusing to start."
    echo "warm-up failed: apt-get update unsuccessful after $WARMUP_TRIES attempts. Fix the proxy/VPN, then re-run." >&2
    exit 1
fi
log "warm-up complete: egress green + apt index warm"

log "run start: runId=$RUN_ID concurrency=$CONCURRENCY pool=$POOL_NAME roster=$ROSTER"

# --- the batch (checkpoint mechanism: done tasks are skipped automatically)
".venv/bin/python" -m bot.eval.harbor.tb21_batch \
    --run-id "$RUN_ID" \
    --dataset "$DATASET" \
    --timeout-multiplier "$TIMEOUT_MULT" \
    --concurrency "$CONCURRENCY" \
    >> "$LOG_DIR/batch.log" 2>&1
BATCH_EXIT=$?
log "batch finished with exit=$BATCH_EXIT"

# --- Post-run sweep: containers/networks/volumes only — task images are
# --- kept on disk across runs (see header note).
docker network prune -f >/dev/null 2>&1 || true
docker volume prune -f >/dev/null 2>&1 || true
docker ps -a --format '{{.Names}}' 2>/dev/null | grep '__env-' | xargs -r docker rm -f >/dev/null 2>&1 || true
IMAGE_COUNT="$(docker images -q | wc -l | tr -d ' ')"
log "post-run cleanup: containers/networks/volumes swept; images preserved ($IMAGE_COUNT images on disk)"
log "jobs/ trajectories preserved under $LOG_DIR/jobs (run evidence)"
log "run end: $RUN_ID"
