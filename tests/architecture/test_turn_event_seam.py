"""Architecture guards for the canonical TurnEvent seam (Scheme C convergence).

These AST-based guards lock the provider-neutral turn-event contract so a
future external provider (Pi, OpenCode, Claude Code, Codex, Cursor, ...)
cannot accidentally regress the convergence by:

1. Importing external types into the WebUI layer (the original
   partial-implementation defect the convergence replaced).
2. Importing WebUI / example-layer types from provider modules (the
   inverse leak).
3. Importing ``ReActEvent`` from provider modules (the coupling the
   convergence removed).
4. Making ``ContentEmitter.emit_turn_event`` abstract (it MUST stay
   concrete with a no-op default so every existing emitter subclass
   remains source-compatible).
5. Importing concrete agent packages from ``core.events`` (core must
   not depend on agent strategies).
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
EXTERNAL_PROVIDERS_ROOT = (
    REPO_ROOT / "src" / "modex_agent" / "agents" / "external" / "providers"
)
EXTERNAL_ROOT = REPO_ROOT / "src" / "modex_agent" / "agents" / "external"
CORE_EVENTS_PATH = REPO_ROOT / "src" / "modex_agent" / "core" / "events.py"
CORE_EMITTER_PATH = REPO_ROOT / "src" / "modex_agent" / "core" / "emitter.py"
WEBUI_EMITTER_DIR = WEBUI_ROOT / "emitter"


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


# ── Guard 3: provider modules do not import ReActEvent ─────────────────────


def test_providers_do_not_import_react_event() -> None:
    """Provider parsers must not couple to ``ReActEvent`` — the canonical
    ``TurnEvent`` seam is the only structured-event contract they feed.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(EXTERNAL_PROVIDERS_ROOT.rglob("*.py")):
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and (
                node.module.endswith("react.agent")
                or node.module.endswith("react")
            ):
                for alias in node.names:
                    if alias.name == "ReActEvent":
                        offenders.setdefault(alias.name, []).append(
                            path.relative_to(EXTERNAL_PROVIDERS_ROOT).as_posix()
                        )
    assert not offenders, (
        f"provider modules must not import ReActEvent (canonical seam): {offenders}"
    )


# ── Guard 4: emit_turn_event stays concrete on ContentEmitter ──────────────


def test_content_emitter_emit_turn_event_is_concrete() -> None:
    """``ContentEmitter.emit_turn_event`` MUST be a concrete method with a
    no-op default (not ``@abstractmethod``) so every existing emitter
    subclass remains source-compatible.
    """
    tree = _parse(CORE_EMITTER_PATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ContentEmitter":
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef)
                    and item.name == "emit_turn_event"
                ):
                    # Must NOT be decorated with @abstractmethod.
                    for dec in item.decorator_list:
                        dec_src = ast.unparse(dec)
                        assert "abstractmethod" not in dec_src, (
                            "ContentEmitter.emit_turn_event must NOT be abstract "
                            "(concrete no-op default preserves emitter compatibility)"
                        )
                    return
    pytest.fail("ContentEmitter.emit_turn_event method not found")


# ── Guard 5: core.events has no import of concrete agent packages ──────────


def test_core_events_does_not_import_concrete_agents() -> None:
    """``core.events`` (and by extension ``core.turn_events``) must not
    import any concrete agent strategy — core is the foundation.
    """
    tree = _parse(CORE_EVENTS_PATH)
    for mod in _imports_from(tree, "modex_agent.agents"):
        pytest.fail(f"core.events must not import agent strategies: {mod}")


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
