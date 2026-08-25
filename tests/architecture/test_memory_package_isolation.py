"""Architecture guard: memory must not import modex_agent.plugins (plan C4).

The ``MemoryProvider`` ABC used to live in ``plugins/abc.py`` and was
reverse-imported by ``memory/recorder.py`` — a memory→plugins dependency
inversion that was also the root cause of a lazy-import cycle in
``plugins/assembly/native_core.py``. The fix (plan
``.omo/plans/slot-rationalization-steps.md`` §2.C4, wave W1.3) moved the ABC
to ``memory/core/provider.py`` and switched recorder.py to a local import.
Since then, plugins (the higher tier) may consume memory — never the
reverse.

AST-based (not substring grep) so comments and string literals cannot
false-positive. ``TYPE_CHECKING``-guarded imports are also flagged: the
inversion is a dependency-direction violation regardless of runtime vs
annotation-only use (``ast.walk`` sees them).

EXPECTED_OFFENDERS lifecycle (tests/AGENTS.md convention): the set holds a
known offender until the fix lands; the assertion is strict set equality,
so BOTH a new violation AND a stale entry (violation fixed but set not
updated) fail. The set is currently EMPTY — keep it that way; new
memory→plugins imports must move the needed ABC down into memory/
instead.
"""
from __future__ import annotations

import ast
from pathlib import Path

MODEX_AGENT_SRC = Path(__file__).resolve().parents[2] / "src" / "modex_agent"
MEMORY_ROOT = MODEX_AGENT_SRC / "memory"

FORBIDDEN_PACKAGE = "modex_agent.plugins"

# Empty since W1.3 (plan §2.C4): memory/recorder.py now imports
# MemoryProvider from memory/core/provider.py. The assertion stays strict.
EXPECTED_OFFENDERS: set[str] = set()


def _plugins_imports(path: Path) -> list[str]:
    """Return the modex_agent.plugins imports found in ``path``.

    Flags ``ast.ImportFrom`` whose module is ``modex_agent.plugins`` or a
    submodule of it, and ``ast.Import`` of the plugins package. Includes
    ``TYPE_CHECKING``-guarded imports.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # node.module is None only for pure relative imports
            # (``from . import x``) — those cannot reach modex_agent.plugins.
            if node.module is not None and (
                node.module == FORBIDDEN_PACKAGE
                or node.module.startswith(FORBIDDEN_PACKAGE + ".")
            ):
                found.append(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == FORBIDDEN_PACKAGE or alias.name.startswith(
                    FORBIDDEN_PACKAGE + "."
                ):
                    found.append(alias.name)
    return sorted(set(found))


def test_memory_package_does_not_import_plugins() -> None:
    offenders: set[str] = set()
    for path in sorted(MEMORY_ROOT.rglob("*.py")):
        if _plugins_imports(path):
            offenders.add(path.relative_to(MODEX_AGENT_SRC).as_posix())
    new = offenders - EXPECTED_OFFENDERS
    stale = EXPECTED_OFFENDERS - offenders
    assert offenders == EXPECTED_OFFENDERS, (
        f"memory→plugins dependency-inversion guard (plan §2.C4). "
        f"scanned src/modex_agent/memory/, offenders={sorted(offenders)!r}, "
        f"expected={sorted(EXPECTED_OFFENDERS)!r}. "
        f"NEW violations {sorted(new)!r}: src/modex_agent/memory/ must not "
        "import modex_agent.plugins — plugins is a consumer of memory, never "
        "the reverse. Move the needed ABC down into memory/ (W1.3 moves "
        "MemoryProvider to memory/core/provider.py) instead of importing "
        "from plugins. "
        f"STALE entries {sorted(stale)!r}: an expected offender was fixed "
        "(e.g. W1.3 landed) but EXPECTED_OFFENDERS was not updated — remove "
        "the entry in the same commit as the fix."
    )
