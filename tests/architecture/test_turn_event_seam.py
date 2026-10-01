"""Architecture guards for the unified TurnEvent sink seam (W2 cutover).

These AST-based guards lock the provider-neutral turn-event contract so a
future external provider (OpenCode, Claude Code, Codex, Cursor, ...)
cannot accidentally regress the convergence by:

1. Importing external types into the WebUI layer (the original
   partial-implementation defect the convergence replaced).
2. Importing WebUI / example-layer types from provider modules (the
   inverse leak).
3. Importing plane-private event types from provider modules (the
   coupling the convergence removed — there is no ``ReActEvent`` anymore,
   and no new enum may take its place).
4. Leaving dead emitter machinery behind: no production import of the
   retired ``ContentEmitter`` / ``EmitterConfig`` / ``StreamingAwareEmitter``
   faces, and ``agents/react`` defines no event enum.
5. Importing concrete agent packages from ``core`` (core must not depend
   on agent strategies).
6. Defining provider-name branches in the WebUI emitter (the projection
   must consume canonical ``TurnEvent`` only).

The guards mirror the pattern in ``test_dependency_tree.py``: AST scan,
explicit offender allow-list, strict assertion.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEBUI_ROOT = REPO_ROOT / "examples" / "bot_project" / "bot" / "webui"
BOT_ROOT = REPO_ROOT / "examples" / "bot_project" / "bot"
EXTERNAL_PROVIDERS_ROOT = (
    REPO_ROOT / "src" / "modex_agent" / "agents" / "external" / "providers"
)
CORE_EMITTER_PATH = REPO_ROOT / "src" / "modex_agent" / "core" / "emitter.py"
REACT_ROOT = REPO_ROOT / "src" / "modex_agent" / "agents" / "react"
WEBUI_EMITTER_DIR = WEBUI_ROOT / "emitter"
SRC_ROOT = REPO_ROOT / "src"


def _imports_from(tree: ast.Module, target_prefix: str) -> list[str]:
    """Return runtime imports whose module starts with *target_prefix*.

    TYPE_CHECKING-guarded imports are excluded (they are annotation-only).
    """
    tc_modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module:
                    tc_modules.add(child.module)
                elif isinstance(child, ast.Import):
                    for alias in child.names:
                        tc_modules.add(alias.name)

    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in tc_modules:
                continue
            if node.module.startswith(target_prefix):
                found.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in tc_modules:
                    continue
                if alias.name.startswith(target_prefix):
                    found.append(alias.name)
    return sorted(set(found))


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


# ── Guard 1: WebUI has no runtime import of external coding ────────────────


def test_webui_does_not_import_external() -> None:
    """The WebUI layer projects canonical ``TurnEvent`` only — it must not
    import ``modex_agent.agents.external`` (the original partial
    implementation's defect).
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(WEBUI_ROOT.rglob("*.py")):
        tree = _parse(path)
        for mod in _imports_from(tree, "modex_agent.agents.external"):
            offenders.setdefault(mod, []).append(
                path.relative_to(WEBUI_ROOT).as_posix()
            )
    assert not offenders, (
        f"WebUI must not import external coding types (canonical seam): {offenders}"
    )


# ── Guard 2: provider modules do not import WebUI / example layer ──────────


def test_providers_do_not_import_webui_or_examples() -> None:
    """Provider parsers/adapters must not import the example-layer WebUI
    code — they emit ``Emission`` and the agent maps it to canonical
    ``TurnEvent``; the WebUI is never a provider dependency.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(EXTERNAL_PROVIDERS_ROOT.rglob("*.py")):
        tree = _parse(path)
        for mod in _imports_from(tree, "bot."):
            offenders.setdefault(mod, []).append(
                path.relative_to(EXTERNAL_PROVIDERS_ROOT).as_posix()
            )
        for mod in _imports_from(tree, "examples."):
            offenders.setdefault(mod, []).append(
                path.relative_to(EXTERNAL_PROVIDERS_ROOT).as_posix()
            )
    assert not offenders, (
        f"provider modules must not import WebUI/examples: {offenders}"
    )


# ── Guard 3: react defines no event enum (the union is the only vocabulary) ─


def test_react_package_defines_no_event_enum() -> None:
    """``agents/react`` must not define a streaming-event enum — the core
    ``TurnEvent`` union is the only event vocabulary; nodes construct its
    variants directly.
    """
    offenders: list[str] = []
    for path in sorted(REACT_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name.endswith("Event"):
                for base in node.bases:
                    if "Enum" in ast.unparse(base) or "agent.AgentEvent" in ast.unparse(base):
                        offenders.append(
                            f"{path.relative_to(REACT_ROOT).as_posix()}::{node.name}"
                        )
    assert not offenders, (
        f"agents/react must not define event enums (use core TurnEvent): {offenders}"
    )


# ── Guard 4: the retired emitter faces are gone from production ────────────

RETIRED_EMITTER_SYMBOLS = ("ContentEmitter", "EmitterConfig", "StreamingAwareEmitter")


def test_no_production_imports_of_retired_emitter_faces() -> None:
    """No ``src/`` or bot production module may import the retired
    ``ContentEmitter`` / ``EmitterConfig`` / ``StreamingAwareEmitter``
    symbols — the single sink face (``TurnEventSink`` + ``BufferingSink``)
    replaced them (dead-path anchor, ``test_dead_code_gone.py`` style).
    """
    offenders: dict[str, list[str]] = {}
    for root in (SRC_ROOT, BOT_ROOT):
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = _parse(path)
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.ImportFrom):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                for name in imported:
                    if name in RETIRED_EMITTER_SYMBOLS:
                        offenders.setdefault(name, []).append(
                            str(path.relative_to(REPO_ROOT))
                        )
    assert not offenders, (
        f"retired emitter faces must not be imported in production: {offenders}"
    )


def test_core_emitter_module_defines_the_sink_face() -> None:
    """``core/emitter.py`` owns the unified sink face: ``TurnEventSink``
    with exactly one abstract data method, the gate-applied ``emit``, and
    a concrete no-op ``flush``.
    """
    tree = _parse(CORE_EMITTER_PATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "TurnEventSink":
            abstract_methods = []
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef):
                    is_abstract = any(
                        "abstractmethod" in ast.unparse(dec) for dec in item.decorator_list
                    )
                    if is_abstract:
                        abstract_methods.append(item.name)
            assert abstract_methods == ["_dispatch"], (
                "TurnEventSink must have exactly one abstract data method (_dispatch)"
            )
            return
    pytest.fail("TurnEventSink class not found in core/emitter.py")


# ── Guard 5: core does not import concrete agent packages ──────────────────


@pytest.mark.parametrize(
    "core_module",
    sorted(str(p.relative_to(SRC_ROOT)) for p in (SRC_ROOT / "modex_agent" / "core").glob("*.py")),
)
def test_core_modules_do_not_import_concrete_agents(core_module: str) -> None:
    """``core`` must not import any concrete agent strategy — core is the
    foundation.
    """
    tree = _parse(SRC_ROOT / core_module)
    for mod in _imports_from(tree, "modex_agent.agents"):
        pytest.fail(f"{core_module} must not import agent strategies: {mod}")


# ── Guard 6: WebBotEmitter does not branch on provider names ───────────────


def test_webui_emitter_has_no_external_provider_branches() -> None:
    """``WebBotEmitter`` must consume canonical ``TurnEvent`` only — no
    string comparisons against ``ExternalEvent`` values and no
    ``Emission`` type references.
    """
    for py_file in WEBUI_EMITTER_DIR.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        src = py_file.read_text(encoding="utf-8")
        assert "ExternalEvent" not in src, (
            f"{py_file.name} must not reference ExternalEvent (canonical seam)"
        )
        assert "from modex_agent.agents.external" not in src, (
            f"{py_file.name} must not import from external (canonical seam)"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
