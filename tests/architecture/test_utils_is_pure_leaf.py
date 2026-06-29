"""Architecture guard: utils is a root-adjacent pure leaf (ADR-0006 policy update
2026-06-26).

`core` may depend on `utils`, but `utils` must not depend on any other internal
package at runtime — only stdlib, third-party, and sibling files within `utils`
itself. Because `utils` cannot point back at its importers, no cycle can form
through it. This guard fails if any `utils/*` file runtime-imports another
`modex_agent.<top-level>` package.

TYPE_CHECKING annotation-only imports are permitted (ADR-0006 scope rule) — they
cannot create a runtime cycle. Candidate ⑥ relocated `message_builder` (which
imported `core`) out of `utils` to satisfy this rule and retired the lazy-import
cycle workaround that had lived at `core/message.py:_user_tz`.
"""
from __future__ import annotations

import ast
from pathlib import Path

UTILS_ROOT = Path(__file__).resolve().parents[2] / "src" / "modex_agent" / "utils"


def _type_checking_modules(tree: ast.Module) -> set[str]:
    """Module names imported under `if TYPE_CHECKING:` — annotation-only, allowed."""
    tc: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module:
                    tc.add(child.module)
    return tc


def _runtime_non_utils_internal(path: Path) -> list[str]:
    """Return `modex_agent.<pkg>` runtime imports where <pkg> is not 'utils'.

    Sibling imports within `utils` (`modex_agent.utils.*`) are allowed; any other
    internal top-level package violates the pure-leaf rule.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tc = _type_checking_modules(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            mod = node.module
            if mod in tc:
                continue
            parts = mod.split(".")
            if (
                len(parts) >= 2
                and parts[0] == "modex_agent"
                and parts[1] != "utils"
            ):
                found.append(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if (
                    len(parts) >= 2
                    and parts[0] == "modex_agent"
                    and parts[1] != "utils"
                ):
                    found.append(alias.name)
    return sorted(set(found))


def test_utils_imports_no_other_internal_package() -> None:
    """ADR-0006 (policy update 2026-06-26): utils is a pure leaf — no runtime
    import of any other modex_agent package."""
    offenders: dict[str, list[str]] = {}
    for path in sorted(UTILS_ROOT.rglob("*.py")):
        for mod in _runtime_non_utils_internal(path):
            offenders.setdefault(mod, []).append(
                path.relative_to(UTILS_ROOT).as_posix()
            )
    assert not offenders, (
        "utils is a pure leaf (ADR-0006, policy update 2026-06-26): it must not "
        "runtime-import any other modex_agent package, else a cycle can form "
        "through it. Offenders:\n  "
        + "\n  ".join(f"{m}: {f}" for m, f in sorted(offenders.items()))
    )
