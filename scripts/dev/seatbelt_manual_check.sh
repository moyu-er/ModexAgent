#!/usr/bin/env bash
# Manual SeatbeltRuntime verification (sandbox-integration Ticket 06).
#
# CI has no macOS runner, so this script is the delivered verification for
# the seatbelt backend. Run ON macOS from anywhere inside the repo:
#
#   bash scripts/dev/seatbelt_manual_check.sh
#
# What it verifies (mirrors the T05 bwrap integration assertions):
#   1. profile compiles: the real compile_seatbelt_profile() output is
#      accepted by `sandbox-exec -n` (dry-run parse)
#   2. echo works inside the sandbox (basic exec)
#   3. reads allowed anywhere (cat a system file)
#   4. write INSIDE the workspace root succeeds
#   5. write OUTSIDE the workspace root is denied (non-zero exit)
#   6. write inside workspace/.git is denied (protected_subpaths shadowing)
#   7. network is denied (curl to example.com fails)
#
# Exit code: 0 = all checks passed, 1 = at least one failed.

set -euo pipefail

[[ "$(uname)" == "Darwin" ]] || { echo "SKIP: macOS only (uname=$(uname))"; exit 0; }
command -v sandbox-exec >/dev/null \
    || { echo "FAIL: sandbox-exec not found on PATH"; exit 1; }
command -v uv >/dev/null \
    || { echo "FAIL: uv not found on PATH (needed to run the profile compiler)"; exit 1; }

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WS="$(mktemp -d "${TMPDIR:-/tmp}/modex-seatbelt-check.XXXXXX")"
trap 'rm -rf "$WS"' EXIT
mkdir -p "$WS/.git"

PROFILE="$WS/profile.sb"

# Compile the WORKSPACE_WRITE / network-off profile with the REAL compiler —
# the script must exercise the shipped code path, not a copy of the profile.
(
    cd "$REPO_ROOT"
    WS="$WS" uv run python - <<'PY' > "$PROFILE.tmp"
import os
from pathlib import Path

from modex_agent.sandbox.seatbelt_runtime import compile_seatbelt_profile
from modex_agent.sandbox.settings import SandboxPolicy, SandboxSettings

settings = SandboxSettings(policy=SandboxPolicy.WORKSPACE_WRITE, network=False)
print(compile_seatbelt_profile(settings, Path(os.environ["WS"])), end="")
PY
) && mv "$PROFILE.tmp" "$PROFILE"

head -c 11 "$PROFILE" | grep -q "(version 1)" \
    || { echo "FAIL: profile does not start with (version 1)"; exit 1; }
echo "--- generated profile ($PROFILE) ---"
cat "$PROFILE"

pass=0
fail=0

# check <description> <expected: ok|deny> <cmd...>
check() {
    local desc="$1" expected="$2"
    shift 2
    local rc=0
    sandbox-exec -f "$PROFILE" "$@" >/dev/null 2>&1 || rc=$?
    local outcome
    if [[ $rc -eq 0 ]]; then outcome="ok"; else outcome="deny"; fi
    if [[ "$outcome" == "$expected" ]]; then
        echo "PASS: $desc ($outcome, rc=$rc)"
        pass=$((pass + 1))
    else
        echo "FAIL: $desc — expected $expected, got $outcome (rc=$rc)"
        fail=$((fail + 1))
    fi
}

echo "--- sandbox-exec assertions ---"

# 1) The profile itself must parse (dry run).
sandbox-exec -n -f "$PROFILE" \
    && echo "PASS: profile compiles (sandbox-exec -n)" && pass=$((pass + 1)) \
    || { echo "FAIL: profile rejected by sandbox-exec -n"; fail=$((fail + 1)); }

# 2) Basic exec: echo runs.
check "echo works" ok /bin/echo ok

# 3) Reads allowed anywhere on the volume.
check "read system file allowed" ok /bin/cat /etc/hosts

# 4) Write inside the workspace root succeeds.
check "write inside workspace allowed" ok /usr/bin/touch "$WS/inside.txt"

# 5) Write outside the workspace root is denied.
check "write outside workspace denied" deny /usr/bin/touch "$WS-outside.txt"

# 6) Protected subpath (.git) is denied even though the workspace is writable.
check "write into workspace/.git denied" deny /usr/bin/touch "$WS/.git/config.tmp"

# 7) Network is denied (network=false).
check "network denied (curl fails)" deny /usr/bin/curl -sS --max-time 5 https://example.com

echo "--- summary: $pass passed, $fail failed ---"
[[ $fail -eq 0 ]] || exit 1
echo "ALL CHECKS PASSED"
