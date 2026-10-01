"""Every import in the modexbot package must resolve in this environment.

The start/restart worker path died at boot with ``ImportError`` because a
function-body (lazy) import named a symbol that had moved to the framework
(``run_with_supervisor``). The unit suite never executes that worker path,
so nothing exercised the import. This anchor parses every module in the
``modexbot`` package — module-level AND function-body imports included —
and imports each one, failing on the first dead link.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

_MODEXBOT_ROOT = Path(__file__).resolve().parents[1] / "modexbot"


def _iter_import_targets(tree: ast.Module) -> list[tuple[str, str | None]]:
    """(module, name) pairs for every import anywhere in the file.

    ``name is None`` means a bare ``import module``; otherwise it is one
    alias of ``from module import name`` — a symbol or a submodule, both
    must resolve (a moved-away name is exactly the failure this anchors).
    """
    targets: list[tuple[str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            for alias in node.names:
                if alias.name != "*":
                    targets.append((node.module, alias.name))
        elif isinstance(node, ast.Import):
            targets.extend((alias.name, None) for alias in node.names)
    return targets


@pytest.mark.parametrize(
    "module_path",
    sorted(p for p in _MODEXBOT_ROOT.glob("*.py")),
    ids=lambda p: p.name,
)
def test_every_modexbot_import_resolves(module_path: Path) -> None:
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for mod, name in _iter_import_targets(tree):
        module = importlib.import_module(mod)
        if name is None:
            continue
        try:
            submodule = importlib.import_module(f"{mod}.{name}")
        except ImportError:
            submodule = None
        assert submodule is not None or hasattr(module, name), (
            f"{module_path.name}: 'from {mod} import {name}' does not resolve "
            f"(neither module '{mod}.{name}' nor attribute on '{mod}')"
        )
