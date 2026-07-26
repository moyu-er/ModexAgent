"""Regression test: modexctl CLI import chain must not pull in the framework.

The ``modexctl`` CLI is a thin HTTP client (``pydantic`` + ``httpx`` only).
``bot/control/__init__.py`` must NOT re-export server-side components
(``BotControlFacade``, ``project_history_messages``) because doing so eagerly
drags in ``bot.webui.transcript_store`` → ``modex_agent`` (the full framework
with all its dependencies). When the CLI is invoked from an external coding
agent subprocess whose environment may not have the full framework installed,
this import chain breaks with ``ModuleNotFoundError``.

This test verifies that importing the CLI's HTTP client module does NOT load
``modex_agent`` — the CLI stays lightweight and side-effect-free.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[6]


def test_http_client_import_does_not_load_modex_agent() -> None:
    """Importing ``bot.cli.modexctl.http_client`` must not load ``modex_agent``.

    The CLI only needs Pydantic wire models from ``bot.control.models`` (a
    pure leaf). If ``bot.control.__init__`` re-exports server-side code, the
    import chain drags in the full framework — this test catches that
    regression by checking ``sys.modules`` after the import.

    Uses subprocess isolation so prior test imports in the same pytest
    process do not mask the leak.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import bot.cli.modexctl.http_client; "
         "assert not any(k.startswith('modex_agent') for k in sys.modules), "
         "f'Framework leak: {[k for k in sys.modules if k.startswith(\"modex_agent\")]}'"],
        capture_output=True, text=True,
        cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": "examples/bot_project"},
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"


def test_control_package_import_does_not_load_modex_agent() -> None:
    """Importing ``bot.control.models`` must not load ``modex_agent``.

    ``bot.control.models`` is a pure leaf (Pydantic + stdlib only). If
    ``bot.control.__init__`` eagerly imports ``facade`` or ``history``, this
    leaf import drags in the framework. This test catches that regression.

    Uses subprocess isolation so prior test imports in the same pytest
    process do not mask the leak.
    """
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; from bot.control.models import ControlError; "
         "assert not any(k.startswith('modex_agent') for k in sys.modules), "
         "f'Framework leak: {[k for k in sys.modules if k.startswith(\"modex_agent\")]}'"],
        capture_output=True, text=True,
        cwd=_REPO_ROOT,
        env={**os.environ, "PYTHONPATH": "examples/bot_project"},
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
