"""Architecture guard: no ``modex_agent`` file imports ``modex_agent.core.graph``.

Per ADR-0033 D13 Stage 4: the old ``src/modex_agent/core/graph/`` directory
is deleted. ReAct migrates to the new ``modex_graph`` package. This test
enforces that no file under ``src/modex_agent/`` imports the deleted module
— a grep-based/AST-based check that fails fast if a future change
re-introduces a reference.

The check inspects import statements via AST parsing (same pattern as
``test_dependency_tree.py`` and ``test_modex_graph_isolation.py``).
``TYPE_CHECKING``-guarded imports are also forbidden — the old module no
longer exists, so even type-checking-only references are stale.
"""

from __future__ import annotations

import ast
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODEX_AGENT_SRC = REPO_ROOT / "src" / "modex_agent"

FORBIDDEN_PREFIX = "modex_agent.core.graph"


def _imports_in_file(path: pathlib.Path) -> set[str]:
    """Parse ``path`` and return the set of module names imported.

    Includes ``TYPE_CHECKING``-guarded imports — the old module is deleted,
    so even type-checking-only references are stale and must be removed.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
    return found


def _forbidden_imports(imports: set[str]) -> set[str]:
    """Filter imports to only those matching the forbidden prefix."""
    return {
        imp
        for imp in imports
        if imp == FORBIDDEN_PREFIX or imp.startswith(FORBIDDEN_PREFIX + ".")
    }


class TestNoCoreGraphImports:
    """No file under ``src/modex_agent/`` may import ``modex_agent.core.graph``."""

    def test_no_modex_agent_core_graph_imports_in_src(self) -> None:
        offenders: dict[str, set[str]] = {}
        for path in sorted(MODEX_AGENT_SRC.rglob("*.py")):
            imports = _imports_in_file(path)
            forbidden = _forbidden_imports(imports)
            if forbidden:
                rel = path.relative_to(MODEX_AGENT_SRC).as_posix()
                offenders[rel] = forbidden
        assert not offenders, (
            "modex_agent.core.graph was deleted (ADR-0033 D13 Stage 4). "
            "No file under src/modex_agent/ may import it. "
            f"Forbidden imports found: {offenders}"
        )
