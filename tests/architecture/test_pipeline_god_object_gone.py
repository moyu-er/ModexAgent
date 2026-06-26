"""pipeline.py no longer defines the extracted responsibilities (DEC-9 guard B).

Semantic inverse of test_dead_code_gone.py: prevents the god-object from creeping back
via re-inlining of execute_turn / process_locked / _handle_snapshot_approval /
_build_runtime_and_context (the candidate-4d extracted methods).
"""
import ast
from pathlib import Path

import pytest

_PIPELINE = Path(__file__).resolve().parents[2] / "src" / "modex_agent" / "pipeline" / "pipeline.py"
_FORBIDDEN_DEFS = [
    "_execute_turn",
    "_process_message_locked",
    "_handle_snapshot_approval",
    "_build_runtime_and_context",
    "_resolve_pool_data",
    "_is_subagent",
]


def _defined_funcs(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


@pytest.mark.parametrize("name", _FORBIDDEN_DEFS)
def test_god_object_method_gone(name: str) -> None:
    source = _PIPELINE.read_text(encoding="utf-8")
    assert name not in _defined_funcs(source), (
        f"pipeline.py re-defines `{name}` — the god-object has regressed"
    )
