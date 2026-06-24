"""ADR-0006 gate: src/modex_agent/core has no runtime upward imports.

TYPE_CHECKING-guarded annotation imports are permitted (ADR-0006 scope).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parents[2] / "src" / "modex_agent" / "core"
TOP_LEVEL = {
    "providers", "commands", "approval", "control", "hook", "interceptor",
    "messaging", "input_pipeline", "adapters", "trace", "memory", "workspace",
    "tools", "sandbox", "runtime", "plugins", "multi_agent", "pipeline", "ioc",
}

# Offenders fixed incrementally by Tasks 3 (engine) and 4 (tool_manager).
# This set shrinks to empty as fixes land; the assertion stays strict.
EXPECTED_OFFENDERS: set[str] = set()


def _type_checking_modules(tree: ast.Module) -> set[str]:
    tc: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module:
                    tc.add(child.module)
    return tc


def _runtime_upward(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tc = _type_checking_modules(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
            if mod in tc:
                continue
            parts = mod.split(".")
            if len(parts) >= 2 and parts[0] == "modex_agent" and parts[1] in TOP_LEVEL:
                found.append(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if len(parts) >= 2 and parts[0] == "modex_agent" and parts[1] in TOP_LEVEL:
                    found.append(alias.name)
    return sorted(set(found))


def test_core_no_unexpected_runtime_upward_imports() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(CORE_ROOT.rglob("*.py")):
        for mod in _runtime_upward(path):
            offenders.setdefault(mod, []).append(path.relative_to(CORE_ROOT).as_posix())
    unexpected = {m: f for m, f in offenders.items() if m not in EXPECTED_OFFENDERS}
    assert not unexpected, f"unexpected runtime upward imports from core: {unexpected}"
