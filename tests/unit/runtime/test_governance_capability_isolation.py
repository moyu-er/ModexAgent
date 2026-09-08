"""Grep-guard: governance SHALL NOT read or write ``model_info``.

ADR-0013 §2 (native-multimodal-inline): the model info carrier is exposed
on ``AgentRuntimeServices`` / ``AgentRuntime`` strictly in PARALLEL to the
governance field — governance must never consume or mutate it.

This test reads every ``ContextGovernance`` subclass file and asserts the
string ``model_info`` does not appear. It is a structural guard so
that a careless import / property access trips CI, not runtime.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_GOVERNANCE_FILES: tuple[str, ...] = (
    "src/modex_agent/memory/context_governance.py",
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("rel", _GOVERNANCE_FILES)
def test_no_governance_file_references_model_info(rel: str) -> None:
    path = _REPO_ROOT / rel
    assert path.is_file(), f"governance file missing: {rel}"
    source = path.read_text(encoding="utf-8")
    assert "model_info" not in source, (
        f"{rel} references 'model_info' — governance SHALL NOT read or "
        f"write that field (ADR-0013 §2)."
    )
