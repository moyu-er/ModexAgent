"""The framework/workspace/ package must have ZERO business coupling.

Static import-lint: AST-walks every module under ``framework/workspace/`` and
asserts no import references business modules or business resource types. This
is the boundary guard that keeps the generic workspace mechanism reusable —
``modex_agent.workspace`` must never import ``bot.*`` or name a concrete business
resource (pool, agent pool, memory system, ...). Business code plugs in via
:class:`modex_agent.workspace.factory.ResourceFactory`.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[3] / "src" / "modex_agent" / "workspace"

# Module-path prefixes whose presence in an import indicates business coupling
# or a forbidden reach into a heavy framework subsystem (workspace is
# foundational). ``modex_agent.pipeline.snapshot`` is allowed: ``resources.py``
# legitimately references the shared ``PoolDataSnapshot`` value type.
MODULE_MARKERS: tuple[str, ...] = (
    "bot.",  # framework must never import business code
    "modex_agent.multi_agent",
    "modex_agent.memory",
    "modex_agent.runtime",
    "modex_agent.ioc",
)

# Concrete business resource types/functions the framework must not name.
# Matched as whole identifiers so ``PoolData`` does NOT match ``PoolDataSnapshot``.
NAME_MARKERS: tuple[str, ...] = (
    "PoolData",
    "PoolSpec",
    "PoolInstance",
    "AgentPool",
    "MemorySystem",
    "create_pool",
)
NAME_RE = re.compile(r"\b(?:" + "|".join(NAME_MARKERS) + r")\b")


def _framework_files() -> list[Path]:
    return [p for p in PKG.rglob("*.py") if "__pycache__" not in p.parts]


def test_framework_package_has_no_business_imports() -> None:
    offenders: list[tuple[str, str]] = []
    for path in _framework_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            for marker in MODULE_MARKERS:
                if marker in module:
                    offenders.append((path.name, marker))
            for alias in (node.names or []):
                if NAME_RE.search(alias.name):
                    offenders.append((path.name, alias.name))
    assert not offenders, f"business coupling in modex_agent.workspace: {offenders}"


def test_framework_package_files_exist() -> None:
    # Sanity: the lint above is meaningful only if it actually scanned the package.
    names = {p.name for p in _framework_files()}
    assert "paths.py" in names
    assert "context.py" in names
    assert "registry.py" in names
    assert "control.py" in names
