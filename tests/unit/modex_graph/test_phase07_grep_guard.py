"""Phase 07 grep guard: retired APIs must never return to production code."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"

# Phase 07 retired modex_graph APIs (suspend_invocation era). Scoped to
# src/modex_graph — NOT all of src/: `_resumed` is a common English word and
# modex_agent has legitimate resume concepts (approval resume, cross-process
# graph recovery), e.g. RootSpanHook._resumed_root (2026-08-18 CI false
# positive). The retired symbols themselves are modex_graph-internal.
FORBIDDEN_PATTERNS = [
    "rebuild_main_state",
    "GraphAsNode",
    "CrashPolicy",
    "_resumed",
]

FORBIDDEN_NODE_PATTERNS = [
    r"\.state_json",
    "state_json=",
    "state_json:",
    r"\.suspended",
    "suspended:",
]


def _grep(pattern: str, path: Path) -> list[str]:
    result = subprocess.run(
        ["git", "grep", "-n", pattern, "--", str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if not result.stdout or not result.stdout.strip():
        return []
    lines = result.stdout.strip().split("\n")
    return [line for line in lines if line.split(":", 1)[0].endswith(".py")]


@pytest.mark.parametrize("pattern", FORBIDDEN_PATTERNS)
def test_forbidden_patterns_absent_in_modex_graph(pattern: str) -> None:
    matches = _grep(pattern, SRC_DIR / "modex_graph")
    assert not matches, (
        f"Forbidden pattern {pattern!r} found in src/modex_graph/:\n" + "\n".join(matches)
    )


@pytest.mark.parametrize("pattern", FORBIDDEN_NODE_PATTERNS)
def test_forbidden_node_patterns_absent_in_modex_graph(pattern: str) -> None:
    matches = _grep(pattern, SRC_DIR / "modex_graph")
    assert not matches, (
        f"Forbidden pattern {pattern!r} found in src/modex_graph/:\n" + "\n".join(matches)
    )
