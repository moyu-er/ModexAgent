"""No pipeline deep-module sub-module holds a code-level back-reference to AgentPipeline (DEC-9 guard A).

Mirrors test_react_nodes_have_no_agent_backref.py (candidate 4c) but uses ast so that
docstring provenance prose (e.g. "Extracted from AgentPipeline._build_*") does NOT
false-fire — only actual code references (imports, annotations, calls, attribute
access) are checked.
"""
import ast
from pathlib import Path

import pytest

_SUB_MODULES = [
    "turn_runner.py",
    "turn_context_builder.py",
    "approval_resumer.py",
    "turn_session_registry.py",
    "dream_scanner.py",
]
_PIPELINE_DIR = Path(__file__).resolve().parents[2] / "src" / "modex_agent" / "pipeline"
_TARGET = "AgentPipeline"


def _code_references_agent_pipeline(source: str) -> list[str]:
    """Return human-readable descriptions of any code-level reference to AgentPipeline.

    Catches: import AgentPipeline / from X import AgentPipeline; Name nodes (use as
    a value/annotation); Attribute access (AgentPipeline.something). Ignores strings
    and comments (ast drops those).
    """
    tree = ast.parse(source)
    hits: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _TARGET or alias.asname == _TARGET:
                    hits.append(f"line {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == _TARGET:
                    hits.append(f"line {node.lineno}: from {node.module} import {alias.name}")
        elif isinstance(node, ast.Name) and node.id == _TARGET:
            hits.append(f"line {node.lineno}: Name '{_TARGET}'")
        elif isinstance(node, ast.Attribute) and node.attr == _TARGET:
            hits.append(f"line {node.lineno}: .{node.attr}")
    return hits


@pytest.mark.parametrize("name", _SUB_MODULES)
def test_no_backref_to_pipeline(name: str) -> None:
    source = (_PIPELINE_DIR / name).read_text(encoding="utf-8")
    hits = _code_references_agent_pipeline(source)
    assert not hits, f"{name} has code-level back-references to {_TARGET}:\n" + "\n".join(hits)
