"""Architecture guard (ticket 11, AC d): the legacy roster road is unreachable.

The scope declaration loader/compiler is the ONLY pool boot path. The
legacy road's modules were deleted with the contract ticket — this guard
fails loudly if they are reintroduced (directly or via import), so no
roster entry point can silently come back.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "modex_agent"
BOT = REPO_ROOT / "examples" / "bot_project" / "bot"

# The legacy roster road's modules — deleted with ticket 11 (contract).
# ``modex_agent.multi_agent.pool_config`` itself stays alive (assembly deps
# + experience/media configs); only its roster faces are dead.
DEAD_MODULES = (
    "modex_agent.config",
    "modex_agent.config.roster",
    "modex_agent.config.loader",
    "modex_agent.config.spec_builder",
    "modex_agent.multi_agent.pool_config.specs",
    "modex_agent.multi_agent.pool_config.store",
)


def _find_spec(name: str):
    """find_spec, treating a missing parent (ModuleNotFoundError) as gone."""
    try:
        return importlib.util.find_spec(name)
    except ModuleNotFoundError:
        return None


def test_legacy_roster_modules_are_gone() -> None:
    for name in DEAD_MODULES:
        assert _find_spec(name) is None, (
            f"legacy roster module {name!r} still exists — the scope "
            "declaration road is the single boot path (ticket 11)"
        )


def test_production_never_imports_the_legacy_road() -> None:
    """AST-level: no src/ or bot/ file imports the deleted modules."""
    offenders: list[str] = []
    for root in (SRC, BOT):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[str] = []
                if isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    targets = [node.module]
                for target in targets:
                    if target in DEAD_MODULES or any(
                        target.startswith(module + ".") for module in DEAD_MODULES
                    ):
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{node.lineno}: {target}"
                        )
    assert not offenders, (
        "production code imports the deleted legacy roster road:\n  "
        + "\n  ".join(offenders)
    )


def test_scope_boot_path_is_importable() -> None:
    """The replacement path exists: the scope loader + compiler (the
    declaration boot's parse chain) and the bot-layer boot wrapper."""
    assert importlib.util.find_spec("modex_agent.scope.loader") is not None
    assert importlib.util.find_spec("modex_agent.scope.compiler") is not None
    assert importlib.util.find_spec("bot.service.pool.declaration") is not None
