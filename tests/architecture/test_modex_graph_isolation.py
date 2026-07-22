"""Architecture guard: modex_graph is framework-agnostic (ADR-0033 D11).

Two layers:

(a) Grep-based: no file under `src/modex_graph/` imports `modex_agent` or `examples/`.
    Uses AST parsing (like `test_dependency_tree.py`) to inspect import statements.

(b) Import-time: `tests/unit/modex_graph/conftest.py` blocks `modex_agent` in
    `sys.modules` via a meta_path finder. If any `modex_graph` submodule
    transitively imports `modex_agent`, the import fails at test collection time.
    This test verifies the blocker is installed.

(c) Dependency declaration: `src/modex_graph/pyproject.toml` must NOT list
    `modex_agent` as a dependency; the root `pyproject.toml` MUST list
    `modex_graph` (or `modex-graph`) as a dependency.
"""
from __future__ import annotations

import ast
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
MODEX_GRAPH_SRC = REPO_ROOT / "src" / "modex_graph"
ROOT_PYPROJECT = REPO_ROOT / "pyproject.toml"
MODEX_GRAPH_PYPROJECT = MODEX_GRAPH_SRC / "pyproject.toml"

FORBIDDEN_IMPORT_PREFIXES = ("modex_agent", "examples")


def _imports_in_file(path: pathlib.Path) -> set[str]:
    """Parse `path` and return the set of module names imported at runtime.

    TYPE_CHECKING-guarded imports are excluded (they're for type checkers only,
    not runtime). String-based `__import__` / `importlib.import_module` calls
    are NOT caught by this AST check — the import-time conftest guard covers
    those at test runtime.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()

    # Collect TYPE_CHECKING-guarded imports to exclude.
    type_checking_modules: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "TYPE_CHECKING":
            for child in ast.walk(node):
                if isinstance(child, ast.ImportFrom) and child.module:
                    type_checking_modules.add(child.module)
                elif isinstance(child, ast.Import):
                    for alias in child.names:
                        type_checking_modules.add(alias.name)

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in type_checking_modules:
                continue
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in type_checking_modules:
                    found.add(alias.name)
    return found


def _forbidden_imports(imports: set[str]) -> set[str]:
    """Filter imports to only those matching forbidden prefixes."""
    return {
        imp
        for imp in imports
        if any(
            imp == prefix or imp.startswith(prefix + ".")
            for prefix in FORBIDDEN_IMPORT_PREFIXES
        )
    }


class TestNoModexAgentImports:
    """Layer (a): grep-based / AST-based check for forbidden imports."""

    def test_no_modex_agent_imports_in_src(self) -> None:
        offenders: dict[str, set[str]] = {}
        for path in sorted(MODEX_GRAPH_SRC.rglob("*.py")):
            imports = _imports_in_file(path)
            forbidden = _forbidden_imports(imports)
            if forbidden:
                rel = path.relative_to(MODEX_GRAPH_SRC).as_posix()
                offenders[rel] = forbidden
        assert not offenders, (
            "modex_graph must not import modex_agent or examples/ (ADR-0033 D11). "
            f"Forbidden imports found: {offenders}"
        )

    def test_no_modex_agent_in_pyproject_dependencies(self) -> None:
        """src/modex_graph/pyproject.toml must NOT list modex_agent as a dependency."""
        if not MODEX_GRAPH_PYPROJECT.exists():
            pytest.skip("src/modex_graph/pyproject.toml does not exist")
        import tomllib

        with open(MODEX_GRAPH_PYPROJECT, "rb") as f:
            data = tomllib.load(f)
        deps = data.get("project", {}).get("dependencies", [])
        for dep in deps:
            dep_lower = dep.lower()
            assert not (
                "modex_agent" in dep_lower or "modexagent" in dep_lower
            ), (
                f"src/modex_graph/pyproject.toml must NOT list modex_agent as a "
                f"dependency (ADR-0033 D11). Found: {dep!r}"
            )

    def test_root_pyproject_lists_modex_graph_dependency(self) -> None:
        """Root pyproject.toml MUST list modex_graph (or modex-graph) as a dependency."""
        text = ROOT_PYPROJECT.read_text(encoding="utf-8")
        assert "modex-graph" in text.lower() or "modex_graph" in text.lower(), (
            "Root pyproject.toml must list modex_graph as a dependency of "
            "modex_agent (ADR-0033 D11)."
        )

    def test_root_pyproject_includes_modex_graph_in_wheel_packages(self) -> None:
        """Root pyproject.toml must include src/modex_graph in wheel packages."""
        text = ROOT_PYPROJECT.read_text(encoding="utf-8")
        assert "src/modex_graph" in text, (
            "Root pyproject.toml must include src/modex_graph in "
            "[tool.hatch.build.targets.wheel] packages."
        )


class TestImportTimeBlocker:
    """Layer (b): the import-time blocker from conftest.py is active."""

    def test_modex_agent_blocker_installed(self) -> None:
        """The conftest.py meta_path blocker is installed."""
        # The blocker is installed by tests/unit/modex_graph/conftest.py.
        # We check that at least one meta_path finder blocks modex_agent.
        # We can't import the conftest directly (it's not a module), but we
        # can verify the blocker works by attempting to import modex_agent
        # and expecting an ImportError.
        #
        # Note: this test only runs if the modex_graph conftest was loaded.
        # Since this file is in tests/architecture/ (not tests/unit/modex_graph/),
        # the conftest may not be loaded. We do a best-effort check.
        #
        # The real import-time guard is exercised when ANY test in
        # tests/unit/modex_graph/ runs — the conftest blocks modex_agent
        # before any modex_graph import resolves.
        #
        # For this architecture test, we verify the conftest file exists
        # and contains the blocker.
        conftest = REPO_ROOT / "tests" / "unit" / "modex_graph" / "conftest.py"
        assert conftest.exists(), "tests/unit/modex_graph/conftest.py must exist"
        text = conftest.read_text(encoding="utf-8")
        assert "_ImportBlocker" in text, (
            "conftest.py must define an _ImportBlocker class that blocks "
            "modex_agent imports (ADR-0033 D11 layer b)."
        )
        assert "modex_agent" in text, (
            "conftest.py must block the 'modex_agent' module prefix."
        )
        assert "meta_path" in text, (
            "conftest.py must insert the blocker into sys.meta_path."
        )

    def test_modex_graph_importable_without_modex_agent(self) -> None:
        """Importing modex_graph must NOT require modex_agent to be importable.

        This test verifies that modex_graph can be imported even when
        modex_agent is explicitly blocked in sys.meta_path.
        """
        # Save state, install blocker, import, restore.
        original_meta_path = sys.meta_path[:]

        class _Blocker:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "modex_agent" or fullname.startswith("modex_agent."):
                    raise ImportError(f"Blocked: {fullname}")
                return None

        blocker = _Blocker()
        sys.meta_path.insert(0, blocker)

        # Clear any cached modex_graph modules to force a fresh import.
        to_remove = [k for k in sys.modules if k.startswith("modex_graph")]
        saved = {k: sys.modules.pop(k) for k in to_remove}

        try:
            import importlib

            import modex_graph
            importlib.reload(modex_graph)
            assert modex_graph.__all__, "modex_graph should have a public surface"
        finally:
            # Restore.
            sys.meta_path[:] = original_meta_path
            # Restore saved modules.
            for k in to_remove:
                if k in saved:
                    sys.modules[k] = saved[k]
                elif k in sys.modules:
                    del sys.modules[k]
