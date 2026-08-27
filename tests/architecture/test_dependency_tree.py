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
#
# Pre-existing ADR-0006 violations (tracked in session_scope_discovery.py
# TODO comment): core/cleanup.py and core/session_scope_discovery.py import
# from workspace.paths, memory.stores.utils, and runtime.store — these are
# upward imports from core (tier 1) to tier 2/3 modules. The fix is a
# dependency inversion (move consumed surfaces down into core, or relocate
# these files out of core). Until that refactor lands, they are listed as
# expected offenders so the architecture gate stays strict for NEW code.
EXPECTED_OFFENDERS: set[str] = {
    "modex_agent.memory.stores.utils",
    "modex_agent.runtime.store",
    "modex_agent.workspace.paths",
    # ADR-0046 todo 13: LLMProvider.chat_stream (core) folds its event
    # stream through EventAssembler (providers/http). Function-body import —
    # module-level would cycle (providers/__init__ -> legacy providers ->
    # core.provider). Resolution direction: EventAssembler is core-pure (only
    # depends on core.*), so relocating it into core/ drops this entry.
    "modex_agent.providers.http.assembler",
}


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


# ── Candidate ③ guards ───────────────────────────────────────────────
WORKSPACE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "modex_agent" / "workspace"
)
MULTI_AGENT_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "modex_agent" / "multi_agent"
)
# tier-3+ top-level modules workspace (tier 2) must not runtime-import.
WORKSPACE_FORBIDDEN_TOP = {"pipeline", "multi_agent", "ioc"}

# Shrinks to empty as fixes land; the assertion stays strict (ADR-0006 pattern).
EXPECTED_WORKSPACE_OFFENDERS: set[str] = set()


def test_workspace_no_runtime_upward_to_tier3plus() -> None:
    """ADR-0006: workspace (tier 2) has no runtime import of tier-3+ modules."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(WORKSPACE_ROOT.rglob("*.py")):
        for mod in _runtime_upward(path):
            top = mod.split(".")[1]
            if top in WORKSPACE_FORBIDDEN_TOP:
                offenders.setdefault(mod, []).append(
                    path.relative_to(WORKSPACE_ROOT).as_posix()
                )
    unexpected = {
        m: f for m, f in offenders.items() if m not in EXPECTED_WORKSPACE_OFFENDERS
    }
    assert not unexpected, (
        f"unexpected runtime upward imports from workspace to tier-3+: {unexpected}"
    )


def test_workspace_manager_not_defined_in_multi_agent() -> None:
    """ADR-0006 candidate ③: WorkspaceManager is a workspace concept; multi_agent
    must not own (define) it. A re-export import is fine; a `class` def is not."""
    offenders: list[str] = []
    for path in sorted(MULTI_AGENT_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "WorkspaceManager":
                offenders.append(path.relative_to(MULTI_AGENT_ROOT).as_posix())
    assert not offenders, f"WorkspaceManager still defined in multi_agent: {offenders}"
