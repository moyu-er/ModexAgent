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
WORKSPACE_FORBIDDEN_TOP = {"pipeline", "multi_agent", "app"}

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


# ── Package layering tree (total order) ──────────────────────────────────
#
# Every top-level package belongs to exactly one level. A runtime import may
# only point to a STRICTLY LOWER level; same-level package imports are
# forbidden. The resulting dependency graph is therefore acyclic.
#
# - `utils` sits below `core`: the pure leaf core may import (ADR-0006).
# - The package root facade (`modex_agent/__init__.py`, `__main__.py`) sits
#   above everything by design — it re-exports the public API surface.
# - TYPE_CHECKING-guarded annotation imports are exempt (ADR-0006 scope),
#   excluded per concrete AST node by `_runtime_upward_modules`.
#
# Violations are pinned in EXPECTED_LAYERING_OFFENDERS as exact (file, module)
# pairs, each annotated with the work wave that removes it; the set must
# shrink to empty and new debt fails the gate.
PACKAGE_LEVELS: dict[str, int] = {
    "utils": -1,
    "core": 0,
    "hook": 1,
    "interceptor": 1,
    # messaging is pure message vocabulary (models + broker + agent-message
    # vocabulary + formatting) — re-leveled 1→0 (W3b) after its broker-bridge
    # composition edge sank to pipeline; this legalizes the level-1
    # adapters/hook/commands imports of message models.
    "messaging": 0,
    "providers": 1,
    "media": 1,
    "workspace": 1,
    "commands": 1,
    "control": 1,
    "adapters": 1,
    # presentation projects the provider-neutral runtime event seam onto a
    # UI-facing event vocabulary + transcript contract (ADR-0053); contracts
    # + default projector only — it consumes core/messaging/utils-level
    # types, never agent strategies.
    "presentation": 1,
    "persistence": 2,
    "memory": 2,
    "trace": 2,
    "runtime": 2,
    "tools": 3,
    "scope": 3,
    "agents": 4,
    "orchestration": 4,
    "pipeline": 5,
    "multi_agent": 6,
    "plugins": 7,
    "acp": 8,
    "app": 8,
}
ROOT_FACADE_LEVEL = 99


def _source_package_of(path: Path) -> str:
    """Top-level package owning `path`; "(root)" for the package-root facade."""
    rel = path.relative_to(PACKAGE_ROOT)
    if len(rel.parts) == 1:
        return "(root)"
    return rel.parts[0]


def _layering_offenders() -> set[tuple[str, str]]:
    """(file, imported-module) pairs violating the strictly-lower-level rule.

    Intra-package imports (target top-level == source top-level) are not the
    gate's concern; core-target imports can never violate (core is level 0
    and is filtered out by ``TOP_LEVEL`` discovery anyway).
    """
    offenders: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        src_pkg = _source_package_of(path)
        src_level = (
            ROOT_FACADE_LEVEL if src_pkg == "(root)" else PACKAGE_LEVELS[src_pkg]
        )
        for mod in _runtime_upward_modules(path, PACKAGE_ROOT):
            tgt_pkg = mod.split(".")[1]
            if tgt_pkg == src_pkg:
                continue
            if PACKAGE_LEVELS[tgt_pkg] >= src_level:
                offenders.add(
                    (path.relative_to(PACKAGE_ROOT).as_posix(), mod)
                )
    return offenders


def test_package_levels_table_covers_every_package() -> None:
    """A newly added top-level package must be assigned a level explicitly."""
    discovered = set(TOP_LEVEL) | {"core"}
    assert discovered == set(PACKAGE_LEVELS), (
        "PACKAGE_LEVELS drift: "
        f"unassigned packages: {sorted(discovered - set(PACKAGE_LEVELS))}, "
        f"stale entries: {sorted(set(PACKAGE_LEVELS) - discovered)}"
    )


# W5 (cleared 2026-09): the template materialization cluster was inverted —
# the materializer seam (AgentMaterializer ABC owned by multi_agent, the
# native implementation injected by the plugins assembly wiring) replaced
# template.py's direct plugins.assembly imports, and the skills capability
# NAME sank to scope/capability.py.
#
# W1-B2 (approval capability bundle): the approval vertical slice moved
# into ``plugins/defaults/capabilities/approval/``. Its below-bundle
# consumers reach the bundle's implementation modules through LAZY
# function-body imports at their use sites — the capability-bundle
# import-light contract (the framework import graph only loads bundle
# implementation modules when approval is actually in play). The pinned
# edges below are exactly that lazy set; the ADR-0051 port-seam
# resolution (ABC below + injection from above, as AgentMaterializer did
# for materialization) removes them and empties this ledger again. Any
# OTHER upward edge remains new debt and fails the gate.
#
# W1-B3 (sandbox capability bundle): the sandbox vertical slice moved
# into ``plugins/defaults/capabilities/sandbox/`` (the top-level
# ``sandbox`` package is deleted). The same import-light contract
# applies: the below-bundle edges below (template/materializer dispatch,
# the delegation-depth budget, workspace tool wrapping, and the web
# tools' SSRF guard) are LAZY function-body imports at their use sites —
# an undeclared deployment never loads the sandbox bundle's
# implementation modules. Intra-plugins edges (assembly sites, the shell
# bundle, approval→sandbox composition) are not this gate's concern.
EXPECTED_LAYERING_OFFENDERS: set[tuple[str, str]] = {
    ("multi_agent/factory.py", "modex_agent.plugins.defaults.capabilities.approval.renderer"),
    ("multi_agent/factory.py", "modex_agent.plugins.defaults.capabilities.approval.resumer"),
    ("multi_agent/template.py", "modex_agent.plugins.defaults.capabilities.approval.security"),
    ("pipeline/turn_context_builder.py", "modex_agent.plugins.defaults.capabilities.approval.response"),
    ("pipeline/turn_runner.py", "modex_agent.plugins.defaults.capabilities.approval.views"),
    # ── W1-B3: the sandbox bundle's below-bundle lazy edges ──
    ("multi_agent/template.py", "modex_agent.plugins.defaults.capabilities.sandbox.decision"),
    ("multi_agent/template.py", "modex_agent.plugins.defaults.capabilities.sandbox.delegation"),
    ("multi_agent/template.py", "modex_agent.plugins.defaults.capabilities.sandbox.settings"),
    ("multi_agent/template.py", "modex_agent.plugins.defaults.capabilities.sandbox.shell_plan"),
    ("multi_agent/template.py", "modex_agent.plugins.defaults.capabilities.sandbox.types"),
    ("multi_agent/tools.py", "modex_agent.plugins.defaults.capabilities.sandbox.delegation"),
    ("tools/web/guarded_http.py", "modex_agent.plugins.defaults.capabilities.sandbox.guard_network"),
    ("tools/web/guarded_transport.py", "modex_agent.plugins.defaults.capabilities.sandbox.guard_network"),
    ("tools/web/reader.py", "modex_agent.plugins.defaults.capabilities.sandbox.guard_network"),
    ("tools/workspace_scoped.py", "modex_agent.plugins.defaults.capabilities.sandbox.tool_matrix"),
}

def test_no_upward_or_same_level_runtime_imports() -> None:
    """Layering tree gate: runtime imports point strictly downward only."""
    offenders = _layering_offenders()
    assert offenders == EXPECTED_LAYERING_OFFENDERS, (
        "package layering ledger mismatch "
        "(every entry must name its removing wave):\n"
        f"  new debt (remove or fix): {sorted(offenders - EXPECTED_LAYERING_OFFENDERS)}\n"
        f"  stale entries (delete, the fix landed): "
        f"{sorted(EXPECTED_LAYERING_OFFENDERS - offenders)}"
    )
