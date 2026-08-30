#!/usr/bin/env pwsh
#Requires -Version 7
# ---------------------------------------------------------------------------
# run-tb21.ps1 — TB2.1 batch launcher (Windows) with automatic post-run cleanup
#
# This is the thin cross-platform launcher around the real entry point:
#   python -m bot.eval.harbor.tb21_batch --run-id <RunId> --dataset <ds> ...
# Run from the repo's .data/ directory, or from anywhere — the script
# resolves the repo root from its own location.
#
# Usage:
#   ./run-tb21.ps1 -RunId <run-name> [-Concurrency 8] [-TimeoutMultiplier 6]
#                  [-PoolName coder] [-Roster benchmark]
#
#   RunId  — the name of the whole test run (e.g. "tb21-all-v7"). It selects
#            the checkpoint/jobs/evidence namespace: tasks already recorded
#            in the run's checkpoint are skipped automatically, so re-running
#            with the same RunId resumes; wiping a task's job dir + evidence
#            + checkpoint row forces that single task to re-run.
#
# Post-run cleanup (always on, results logged to batch.log):
#   1. docker network prune   — trial networks the --delete teardown missed
#   2. docker volume prune -f — unreferenced task volumes
#   3. stale __env-* container sweep
#   IMAGES ARE NEVER DELETED: docker.io is unreachable from this region and
#   the task-image set (~60 GB) must survive across runs. Trial image pulls
#   go through the daemon registry-mirrors transparently.
# Trajectories (jobs/<task>/...) are always kept — they are the run evidence.
#
# NOT automated (host-level, needs elevation — run manually when disk fills;
# the docker data vhdx is sparse and never shrinks on its own):
#   wsl --shutdown
#   Optimize-VHD -Path "<docker-desktop data vhdx>" -Mode Full
# ---------------------------------------------------------------------------
param(
    [Parameter(Mandatory = $true)]
    [string]$RunId,
    [int]$Concurrency = 8,
    [int]$TimeoutMultiplier = 6,
    [string]$PoolName = "coder",
    [string]$Roster = "benchmark",
    # Optional comma-separated task subset (e.g. "fix-git,torch-pipeline-parallelism").
    # Implemented as a micro-dataset of junction-linked task dirs under
    # .data/tb21-runs/<RunId>-dataset/ — the batch sees only these tasks.
    [string]$Tasks = ""
)
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $PSCommandPath
# <repo>/examples/bot_project/bot/eval/run-tb21.ps1 -> repo root is four levels up
$repo = (Resolve-Path (Join-Path $scriptDir "..\..\..\..")).Path
$bot = Join-Path $repo "examples\bot_project"
$dataset = Join-Path $repo ".data\terminal-bench-2-1"
$logDir = Join-Path $repo ".data\tb21-runs\$RunId"

if (-not (Test-Path (Join-Path $bot ".venv"))) {
    throw "bot venv not found at $bot\.venv — run install first"
}
if (-not (Test-Path $dataset)) {
    throw "dataset not found at $dataset"
}

Set-Location $bot
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Log([string]$msg) {
    $msg | Add-Content -Path (Join-Path $logDir "batch.log") -Encoding utf8
}

# --- Optional task subset (-Tasks "a,b"): junction-link a micro-dataset so
# --- the batch sees only the named tasks (fresh RunId => no checkpoint rows).
if ($Tasks) {
    $micro = Join-Path $repo ".data\tb21-runs\$RunId-dataset"
    if (Test-Path $micro) { Remove-Item -Recurse -Force $micro }
    New-Item -ItemType Directory -Path $micro -Force | Out-Null
    foreach ($t in ($Tasks -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $src = Join-Path $dataset $t
        if (-not (Test-Path (Join-Path $src "task.toml"))) {
            throw "unknown task '$t' (no task.toml under $src)"
        }
        New-Item -ItemType Junction -Path (Join-Path $micro $t) -Target $src | Out-Null
    }
    $dataset = $micro
    Log "micro-dataset for run $RunId : $Tasks"
}

# --- env: .env is the source of truth (langfuse/otel endpoints + auth);
# --- strip stale model overrides so model.yml stays the single source.
Get-Content ".env" | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        Set-Item -Path "Env:$($matches[1].Trim())" -Value $matches[2].Trim().Trim('"').Trim("'")
    }
}
foreach ($v in @("LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "MODEX_MAX_CONTEXT_TOKENS", "MODEX_MAX_OUTPUT_TOKENS")) {
    Remove-Item -Path "Env:$v" -ErrorAction SilentlyContinue
}
$env:MODEX_POOL_NAME = $PoolName
$env:MODEX_EVAL_ROSTER = $Roster
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

# --- The host-side process must bypass any configured system proxy so
# --- langfuse/otel traffic goes direct; the trial containers (docker
# --- network) keep their own proxy routing.
foreach ($v in @("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy")) {
    Remove-Item -Path "Env:$v" -ErrorAction SilentlyContinue
}
$env:NO_PROXY = "*"
$env:no_proxy = "*"

# --- Pre-flight sweep: force-killed runs leak trial containers + compose
# --- networks; stale networks exhaust Docker's address pool.
$stale = docker ps -a --format "{{.Names}}" | Where-Object { $_ -match "__env-" }
foreach ($c in $stale) { docker rm -f $c | Out-Null }
if ($stale) { Log "swept $($stale.Count) stale trial containers" }
docker network prune -f | Out-Null

# --- Warm-up (mandatory, before the batch): the trial agent install path
# --- needs apt/pypi/github through the Docker proxy bridge. A cold or
# --- flapping proxy makes 8 concurrent `apt-get update`s time out and EVERY
# --- task dies at install ("no_python_runtime") — observed on tb21-all-v7
# --- (2026-08-25). Each step retries up to 3 times (VPN nodes flap
# --- transiently); all attempts failing means the network is truly down.
$warmupTries = 3

# pipidx probes the EFFECTIVE install index, not a hardcoded mirror: the
# trial's uv/pip install resolves --index-url from MODEX_PIP_INDEX (forwarded
# into the trial container; default mirrors bot/eval/harbor/agent.py
# DEFAULT_PIP_INDEX). A stale override (tb21-all-v8 attempt3, 2026-08-30:
# MODEX_PIP_INDEX=pypi.org leaked from the launching shell) must gate on its
# own index — the green aliyun probe did not cover it and every install died.
$idxUrl = if ($env:MODEX_PIP_INDEX) { $env:MODEX_PIP_INDEX } else { "https://mirrors.aliyun.com/pypi/simple/" }

function Invoke-Probe {
    # uvdist: full 21MB tarball download — the exact transfer the TB verifiers
    # die on (curl(18) partial file) when a VPN node flap truncates a
    # large-file HTTPS transfer. A homepage probe (astral.sh) never catches it.
    docker run --rm -e IDX_URL="$idxUrl" curlimages/curl:latest sh -c 'for t in "https://api.deepseek.com/v1/models|llm" "$IDX_URL|pipidx" "http://archive.ubuntu.com/ubuntu/dists/noble/InRelease|apt" "https://github.com/astral-sh/uv/releases/download/0.9.5/uv-x86_64-unknown-linux-gnu.tar.gz|uvdist"; do url="${t%%|*}"; name="${t##*|}"; code=$(curl -sL -o /dev/null -w "%{http_code} %{size_download}" --max-time 90 "$url"); echo "$name=$code"; done' 2>$null
}
$probeOk = $false
for ($try = 1; $try -le $warmupTries; $try++) {
    Log "warm-up: probing container egress (attempt $try/$warmupTries)"
    # name=<code> <bytes>: the uvdist probe must transfer the full tarball
    # (21370871 bytes) — "200 21370871" — not merely return HTTP 200. Any
    # truncated/partial transfer fails the gate even with a 200 code.
    $probeLines = @(Invoke-Probe | ForEach-Object {
        if ($_ -match '^([a-z]+)=(\d+) (\d+)$') {
            $n = $matches[1]; $c = $matches[2]; $b = [long]$matches[3]
            if ($n -eq 'uvdist' -and $c -eq '200' -and $b -ne 21370871) { "$n=$($c)truncated($($b)B)" } else { "$n=$c" }
        } elseif ($_ -match '^([a-z]+)=(\d+)$') { $_ }
    })
    $probeLines | ForEach-Object { Log "warm-up probe: $_" }
    $bad = @($probeLines | Where-Object { $_ -notmatch "^(llm=(200|401)|pipidx=200|apt=200|uvdist=200)$" })
    if ($bad.Count -eq 0 -and $probeLines.Count -eq 4) { $probeOk = $true; break }
    Log "warm-up probe attempt $try failed: $($bad -join ', ')"
}
if (-not $probeOk) {
    Log "warm-up FAILED: container egress not ready after $warmupTries attempts. Refusing to start."
    throw "warm-up failed: container egress not ready after $warmupTries attempts. Fix the proxy/VPN, then re-run."
}

# apt index warm-up: pull the apt indexes through the proxy once, serially,
# so the batch's concurrent installs don't cold-start it. Uses the FIRST
# TASK'S OWN IMAGE (the trial's ubuntu base) — no hardcoded helper image;
# with -Tasks it is exactly the image the rerun will use.
$warmImage = ($Tasks -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -First 1)
if (-not $warmImage) { $warmImage = (Get-ChildItem $dataset -Directory | Sort-Object Name | Select-Object -First 1).Name }
$warmRef = "alexgshaw/${warmImage}:20251031"
Log "warm-up: apt index pull (serial, image $warmRef)"
$aptOk = $false
for ($try = 1; $try -le $warmupTries; $try++) {
    $aptWarm = docker run --rm $warmRef sh -c 'apt-get update -qq >/dev/null 2>&1; echo "apt_update_exit=$?"' 2>$null
    Log "warm-up apt attempt ${try}: $aptWarm"
    if ($aptWarm -match "apt_update_exit=0") { $aptOk = $true; break }
}
if (-not $aptOk) {
    Log "warm-up FAILED: apt-get update did not succeed after $warmupTries attempts. Refusing to start."
    throw "warm-up failed: apt-get update unsuccessful after $warmupTries attempts. Fix the proxy/VPN, then re-run."
}
Log "warm-up complete: egress green + apt index warm"

Log "run start: runId=$RunId concurrency=$Concurrency pool=$PoolName roster=$Roster"

& ".venv\Scripts\python.exe" -m bot.eval.harbor.tb21_batch `
    --run-id $RunId `
    --dataset $dataset `
    --timeout-multiplier $TimeoutMultiplier `
    --concurrency $Concurrency `
    *> (Join-Path $logDir "batch.log.append")
Get-Content (Join-Path $logDir "batch.log.append") -ErrorAction SilentlyContinue | Add-Content (Join-Path $logDir "batch.log")
Remove-Item (Join-Path $logDir "batch.log.append") -ErrorAction SilentlyContinue
Log "batch finished with exit=$LASTEXITCODE"

# --- Post-run sweep: anything the batch's own --delete teardown missed.
# --- Containers/networks/volumes are reclaimed, but IMAGES ARE NEVER
# --- DELETED: docker.io is unreachable from this region and a full task-
# --- image set (~60 GB) must survive across runs. Pulls are transparent
# --- through the daemon registry-mirrors (docker.xuanyuan.run / 1ms.run).
docker network prune -f | Out-Null
docker volume prune -f | Out-Null
$stalePost = docker ps -a --format "{{.Names}}" | Where-Object { $_ -match "__env-" }
foreach ($c in $stalePost) { docker rm -f $c | Out-Null }
Log "post-run cleanup: containers/networks/volumes swept; images preserved ($((docker images -q | Measure-Object).Count) images on disk)"
Log "jobs/ trajectories preserved under $logDir\jobs (run evidence)"
Log "run end: $RunId"
