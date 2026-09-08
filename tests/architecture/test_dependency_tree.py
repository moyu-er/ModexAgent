"""ADR-0006 gate: src/modex_agent/core has no runtime upward imports.

TYPE_CHECKING-guarded annotation imports are permitted (ADR-0006 scope) —
excluded per concrete AST node, not per module name, so a runtime import of
the same module elsewhere in the same file still counts.

The scanner resolves every import form a real edge can hide behind
(ARCHITECTURE-MIGRATION-PLAN.md A1; ADR-0006 "Current dependency leakage and
disposition"):

- absolute ``modex_agent.<pkg>...`` imports,
- relative imports (``from . import x`` / ``from ..types import y``) resolved
  against the importing file's package,
- imports anywhere in the file — module body, function bodies, ``if`` blocks,
  ``try/except ImportError`` blocks — via a full ``ast.walk``,
- top-level packages are auto-discovered from ``src/modex_agent/`` at runtime
  (any directory with ``__init__.py`` except ``core`` itself), so a new
  package can never silently fall outside the guard.

``utils`` is the one permitted internal import from core (ADR-0006
"root-adjacent pure leaf" policy) and is never an offender.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
PACKAGE_ROOT = SRC_ROOT / "modex_agent"
CORE_ROOT = PACKAGE_ROOT / "core"
TOOLS_ROOT = PACKAGE_ROOT / "tools"


def _discover_top_level_packages() -> frozenset[str]:
    """Every directory under src/modex_agent/ with an __init__.py, minus core.

    Runtime discovery (A1): a newly added top-level package is covered by the
    guard without a hand-maintained list drifting out of date.
    """
    return frozenset(
        entry.name
        for entry in PACKAGE_ROOT.iterdir()
        if entry.is_dir()
        and entry.name != "__pycache__"
        and entry.name != "core"
        and (entry / "__init__.py").is_file()
    )


TOP_LEVEL = _discover_top_level_packages()

# Offenders fixed incrementally by work packages B1-B4, C1, E1 of the
# architecture-convergence migration (ARCHITECTURE-MIGRATION-PLAN.md §15;
# the ground-truth table is ADR-0006 "Current dependency leakage and
# disposition"). Each entry is the (file, imported module) runtime edge; the
# set shrinks to empty as work packages land, and the assertion stays an
# exact match so no new debt can pass unnoticed.
#
# utils is never listed: core MAY import utils (ADR-0006 pure-leaf policy).
# C1 removed both media edges: MediaStore/Attachment contracts were promoted
# to core/media.py; media/store.py keeps only LocalFileMediaStore.
EXPECTED_OFFENDERS: set[tuple[str, str]] = set()


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    """ids() of Import/ImportFrom nodes syntactically inside `if TYPE_CHECKING:`.

    Node-level exclusion (A1): only the concrete nodes under the TYPE_CHECKING
    guard are permitted; a runtime import of the same module elsewhere in the
    file is NOT excluded. ast.walk covers every statement nested in the If,
    and TYPE_CHECKING blocks at any depth (not just module body). Imports in
    the guard's ``else`` branch remain runtime imports.
    """
    tc_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for statement in node.body:
                for child in ast.walk(statement):
                    if isinstance(child, ast.Import | ast.ImportFrom):
                        tc_nodes.add(id(child))
    return tc_nodes


def _module_of(path: Path, root: Path) -> str:
    """Dotted module path of `path` relative to `root` (src/)."""
    rel = path.relative_to(root)
    return ".".join(("modex_agent", *rel.with_suffix("").parts))


def _package_of(path: Path, root: Path) -> str:
    """Dotted package path containing `path` — what `from .` resolves against.

    ``pkg/mod.py`` lives in package ``pkg``; ``pkg/__init__.py`` IS package
    ``pkg``.
    """
    parts = _module_of(path, root).split(".")
    if path.name == "__init__.py":
        return ".".join(parts)
    return ".".join(parts[:-1])


def _is_module_on_disk(module: str) -> bool:
    """True if `module` names a real module/package file under src/."""
    parts = module.split(".")
    if not parts or parts[0] != "modex_agent":
        return False
    rel = Path(*parts[1:])
    return (
        (PACKAGE_ROOT / rel.with_suffix(".py")).is_file()
        or (PACKAGE_ROOT / rel / "__init__.py").is_file()
    )


def _iter_import_targets(
    node: ast.ImportFrom | ast.Import, package: str
) -> list[str]:
    """Absolute module paths imported by `node`, relative imports resolved.

    For ``from <pkg> import x`` each alias is also probed as a submodule
    target (``from modex_agent import memory`` must count), but only when the
    compound path is a real module on disk — ``from modex_agent.runtime.store
    import JsonFileTodoStore`` imports a class symbol, not the module
    ``...JsonFileTodoStore``.
    """
    mods: list[str] = []
    if isinstance(node, ast.ImportFrom):
        if node.level == 0:
            base = node.module or ""
        else:
            # Resolve `level` dots against the importing file's package:
            # level=1 is the package itself, each extra dot climbs once. An
            # over-climb past "modex_agent" is a broken import, never an
            # internal-package target.
            parts = package.split(".")
            climb = node.level - 1
            if climb > len(parts):
                return []
            base = ".".join(parts[: len(parts) - climb])
            if node.module:
                base = f"{base}.{node.module}" if base else node.module
        if not base:
            return []
        mods.append(base)
        for alias in node.names:
            if alias.name != "*":
                candidate = f"{base}.{alias.name}"
                if _is_module_on_disk(candidate):
                    mods.append(candidate)
    else:
        for alias in node.names:
            mods.append(alias.name)
    return mods


def _runtime_upward_modules(path: Path, root: Path) -> list[str]:
    """Runtime internal upward imports (absolute module paths) in `path`.

    Every import form counts: absolute, relative, module-level, function-body,
    try/except ImportError, if-blocks. Only concrete TYPE_CHECKING AST nodes
    are excluded.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tc_nodes = _type_checking_nodes(tree)
    package = _package_of(path, root)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if id(node) in tc_nodes:
            continue
        for mod in _iter_import_targets(node, package):
            parts = mod.split(".")
            if (
                len(parts) >= 2
                and parts[0] == "modex_agent"
                and parts[1] in TOP_LEVEL
            ):
                found.add(mod)
    return sorted(found)


def test_core_no_unexpected_runtime_upward_imports() -> None:
    """ADR-0006: core imports no top-level package at runtime except utils.

    Exact-match ledger (plan A1): every current offender is pinned as a
    (file, module) pair so the set shrinks deliberately — a fix that lands
    without deleting its entry fails the test, and new debt fails harder.
    """
    offenders: set[tuple[str, str]] = set()
    for path in sorted(CORE_ROOT.rglob("*.py")):
        file_rel = path.relative_to(CORE_ROOT).as_posix()
        for mod in _runtime_upward_modules(path, PACKAGE_ROOT):
            if mod.split(".")[1] != "utils":
                offenders.add((file_rel, mod))
    assert offenders == EXPECTED_OFFENDERS, (
        "core runtime-upward-import ledger mismatch "
        "(ARCHITECTURE-MIGRATION-PLAN.md A1; every entry must name its "
        "removing work package B1-B4/C1/E1):\n"
        f"  new debt (remove or fix): {sorted(offenders - EXPECTED_OFFENDERS)}\n"
        f"  stale entries (delete, the fix landed): "
        f"{sorted(EXPECTED_OFFENDERS - offenders)}"
    )


def test_import_scanner_resolves_absolute_alias_submodule(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text("from modex_agent import memory\n", encoding="utf-8")

    assert "modex_agent.memory" in _runtime_upward_modules(probe, tmp_path)


def test_import_scanner_resolves_relative_alias_submodule(tmp_path: Path) -> None:
    package = tmp_path / "core"
    package.mkdir()
    probe = package / "probe.py"
    probe.write_text("from .. import memory\n", encoding="utf-8")

    assert "modex_agent.memory" in _runtime_upward_modules(probe, tmp_path)


def test_import_scanner_keeps_type_checking_else_runtime_import(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from modex_agent import commands\n"
        "else:\n"
        "    from modex_agent import memory\n",
        encoding="utf-8",
    )

    imports = _runtime_upward_modules(probe, tmp_path)
    assert "modex_agent.commands" not in imports
    assert "modex_agent.memory" in imports


EXPECTED_TOOLS_AGENT_OFFENDERS: set[tuple[str, str]] = set()


def test_tools_no_unexpected_runtime_imports_of_agents() -> None:
    offenders: set[tuple[str, str]] = set()
    for path in sorted(TOOLS_ROOT.rglob("*.py")):
        file_rel = path.relative_to(TOOLS_ROOT).as_posix()
        for mod in _runtime_upward_modules(path, PACKAGE_ROOT):
            if mod.split(".")[1] == "agents":
                offenders.add((file_rel, mod))
    assert offenders == EXPECTED_TOOLS_AGENT_OFFENDERS, (
        "tools-to-agents runtime-import ledger mismatch:\n"
        f"  new debt (remove or fix): "
        f"{sorted(offenders - EXPECTED_TOOLS_AGENT_OFFENDERS)}\n"
        f"  stale entries (delete, the fix landed): "
        f"{sorted(EXPECTED_TOOLS_AGENT_OFFENDERS - offenders)}"
    )


# ── Candidate ③ guards ───────────────────────────────────────────────
WORKSPACE_ROOT = PACKAGE_ROOT / "workspace"
MULTI_AGENT_ROOT = PACKAGE_ROOT / "multi_agent"
# tier-3+ top-level modules workspace (tier 2) must not runtime-import.
WORKSPACE_FORBIDDEN_TOP = {"pipeline", "multi_agent", "ioc"}

# Shrinks to empty as fixes land; the assertion stays strict (ADR-0006 pattern).
EXPECTED_WORKSPACE_OFFENDERS: set[str] = set()


def test_workspace_no_runtime_upward_to_tier3plus() -> None:
    """ADR-0006: workspace (tier 2) has no runtime import of tier-3+ modules."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(WORKSPACE_ROOT.rglob("*.py")):
        for mod in _runtime_upward_modules(path, PACKAGE_ROOT):
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
