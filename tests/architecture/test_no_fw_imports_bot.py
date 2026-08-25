"""Rule 9 gate: src/modex_agent never imports bot.* or modexbot.*.

The framework (``src/modex_agent/``) must stay independent of business
code (``examples/bot_project/``). Pool execution strategies and other
bot components live in the example project; the framework holds only
the ``ExecutionStrategy`` ABC (``multi_agent/execution_strategy.py``).

AST-based (not substring grep) so comments and string literals cannot
false-positive. Zero exceptions — no whitelist.
"""
from __future__ import annotations

import ast
from pathlib import Path

FW_ROOT = Path(__file__).resolve().parents[2] / "src" / "modex_agent"

FORBIDDEN_BARE = {"bot", "modexbot"}
FORBIDDEN_PREFIXES = ("bot.", "modexbot.")


def _is_forbidden(module: str) -> bool:
    return module in FORBIDDEN_BARE or module.startswith(FORBIDDEN_PREFIXES)


def _forbidden_bot_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_forbidden(alias.name):
                    found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # node.module is None only for pure relative imports
            # (``from . import x``) — those cannot reach bot.*.
            if node.module is not None and _is_forbidden(node.module):
                found.append(node.module)
    return sorted(set(found))


def test_framework_never_imports_bot_modules() -> None:
    offenders: dict[str, list[str]] = {}
    for path in sorted(FW_ROOT.rglob("*.py")):
        for mod in _forbidden_bot_imports(path):
            offenders.setdefault(mod, []).append(path.relative_to(FW_ROOT).as_posix())
    assert not offenders, (
        f"src/modex_agent imports business modules (bot.*/modexbot.*): {offenders}"
    )
