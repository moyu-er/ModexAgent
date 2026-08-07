"""Test configuration for modex_graph unit tests.

Per ADR-0033 D11 + acceptance criteria: this conftest blocks `modex_agent`
imports that originate from `modex_graph` modules. This is the import-time
layer of the architecture guard — if any `modex_graph` file transitively
imports `modex_agent`, the import fails here.

The blocker is scoped: it only fires when the import call chain includes a
`modex_graph.*` module. This allows modex_agent tests to run in the same
pytest session without interference.

The grep-based layer lives in `tests/architecture/test_modex_graph_isolation.py`.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Sequence
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from types import ModuleType

# Add this test directory to sys.path so test files can `from helpers import ...`.
_TEST_DIR = str(pathlib.Path(__file__).parent)
if _TEST_DIR not in sys.path:
    sys.path.insert(0, _TEST_DIR)


def _originates_from_modex_graph() -> bool:
    """Check if the current import call chain includes a modex_graph module."""
    import inspect

    frame = inspect.currentframe()
    while frame is not None:
        module_name = frame.f_globals.get("__name__", "")
        if module_name == "modex_graph" or module_name.startswith("modex_graph."):
            return True
        frame = frame.f_back
    return False


class _ImportBlocker(MetaPathFinder):
    """Block `modex_agent` imports that originate from `modex_graph` modules.

    Inserted into `sys.meta_path` as a finder. When `import modex_agent` is
    attempted AND the import call chain includes a `modex_graph.*` module,
    raises `ImportError`. This catches transitive imports of modex_agent from
    within modex_graph without blocking modex_agent's own test suite.
    """

    def __init__(self, blocked_prefix: str) -> None:
        self._blocked = blocked_prefix

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if (
            fullname == self._blocked or fullname.startswith(self._blocked + ".")
        ) and _originates_from_modex_graph():
            raise ImportError(
                f"Architecture violation: {self._blocked!r} is blocked when "
                f"imported from modex_graph (ADR-0033 D11). modex_graph must "
                f"not import modex_agent. Attempted import: {fullname!r}."
            )
        return None


_BLOCKER = _ImportBlocker("modex_agent")
if not any(isinstance(f, _ImportBlocker) for f in sys.meta_path):
    sys.meta_path.insert(0, _BLOCKER)
