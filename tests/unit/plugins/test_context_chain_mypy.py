"""AC (c) meta-test: the declared ``create()`` layer is the capability
boundary, asserted at the TYPE level by running mypy on fixtures.

The fixtures live in ``mypy_fixtures/`` (not collected by pytest — no
``test_`` prefix). Each fixture is a complete factory declaration whose
ONLY variable is the declared ctx layer vs. the fields it reads:

- negative — ``PoolContext``-declared factory reading workspace-layer
  fields → mypy MUST fail (non-zero exit + attribute errors naming the
  unreachable fields).
- positive — ``PoolContext``-declared factory reading pool-layer data
  (SPEC §3.3 todo-factory shape), ``WorkspaceContext``-declared factory
  reading paths + the MCP shared handle (AC (d)), and a legacy
  BIZ-style ``AssemblyContext`` factory (unchanged pattern) → mypy MUST
  pass (exit 0).

mypy runs with the project's own configuration (cwd = repo root, so
``pyproject.toml`` ``[tool.mypy]`` applies) — the same gate the ticket
uses. ``MYPYPATH=src`` is required so mypy resolves ``modex_agent``
from the src layout for files OUTSIDE the package tree (the editable
install's .pth mechanism is invisible to mypy; without it every
``import modex_agent`` silently degrades to ``Any`` and the assertions
below would vacuously pass).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURES = Path(__file__).resolve().parent / "mypy_fixtures"


def _run_mypy(fixture_name: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["MYPYPATH"] = str(_REPO_ROOT / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--no-error-summary",
            str(_FIXTURES / fixture_name),
        ],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        env=env,
        check=False,
    )


def test_pool_scoped_factory_reading_workspace_fields_is_type_error() -> None:
    """AC (c): a PoolContext-declared factory reading workspace-layer
    fields (path layout, MCP handle) fails mypy with attribute errors."""
    result = _run_mypy("pool_factory_reads_workspace.py")

    assert result.returncode != 0, (
        "mypy accepted a pool-scoped factory reading workspace-layer "
        f"fields — the capability boundary is broken:\n{result.stdout}"
    )
    assert '"PoolContext" has no attribute "workspace_ctx"' in result.stdout
    assert '"PoolContext" has no attribute "mcp_registry"' in result.stdout


def test_pool_scoped_factory_reading_pool_layer_typechecks() -> None:
    """SPEC §3.3 todo-factory shape: declaring PoolContext and reading
    pool-layer data is clean."""
    result = _run_mypy("pool_factory_reads_pool.py")

    assert result.returncode == 0, result.stdout


def test_workspace_scoped_factory_reads_paths_and_mcp_handle() -> None:
    """AC (d): the path layout AND the MCP shared handle are reachable
    via a WorkspaceContext declaration."""
    result = _run_mypy("workspace_factory_reads_paths_and_mcp.py")

    assert result.returncode == 0, result.stdout


def test_legacy_biz_style_factory_declaration_still_typechecks() -> None:
    """BIZ factories written against the pre-ticket pattern (bare
    ``ComponentFactory`` subclass, ``ctx: AssemblyContext``) typecheck
    unchanged — override variance accepts the widened parameter."""
    result = _run_mypy("legacy_factory_reads_assembly_context.py")

    assert result.returncode == 0, result.stdout
