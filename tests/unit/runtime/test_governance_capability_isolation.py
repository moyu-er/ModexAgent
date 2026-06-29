"""Grep-guard: governance SHALL NOT read or write ``model_capabilities``.

ADR-0013 §2 (native-multimodal-inline): the capability carrier is exposed
on ``AgentRuntimeServices`` / ``AgentRuntime`` strictly in PARALLEL to the
governance field — governance must never consume or mutate it. A future
inline renderer binds to it instead.

This test reads every ``ContextGovernance`` subclass file and asserts the
string ``model_capabilities`` does not appear. It is a structural guard so
that a careless import / property access trips CI, not runtime.
"""
from __future__ import annotations

from pathlib import Path

import pytest

# Every file that declares a ContextGovernance subclass. The ABC itself lives
# in core/governance.py; concrete subclasses live in memory/context_governance.py.
# Keep this list explicit — a new subclass file must be added here so the guard
# covers it.
_GOVERNANCE_FILES: tuple[str, ...] = (
    "src/modex_agent/core/governance.py",
    "src/modex_agent/memory/context_governance.py",
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("rel", _GOVERNANCE_FILES)
def test_no_governance_file_references_model_capabilities(rel: str) -> None:
    """No ContextGovernance file mentions model_capabilities (read or write)."""
    path = _REPO_ROOT / rel
    assert path.is_file(), f"governance file missing: {rel}"
    source = path.read_text(encoding="utf-8")
    assert "model_capabilities" not in source, (
        f"{rel} references 'model_capabilities' — governance SHALL NOT read or "
        f"write that field (ADR-0013 §2). Bind the inline renderer to "
        f"ctx.runtime.model_capabilities instead."
    )
