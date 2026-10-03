"""W1-B3 net-new pin: the sandbox bundle's import-light facade.

Importing the registration entry must NOT eagerly import the bundle's
implementation modules (``decision``, the guard family, ``interceptor``,
``runtime``, ``adapters``, the OCI/bwrap/Seatbelt runtimes) nor the
approval bundle's heavy modules — the framework import graph only loads
bundle implementation modules when sandbox is actually in play. Pinned
via ``sys.modules`` in a subprocess (the same pattern as the approval
bundle's facade test).
"""

from __future__ import annotations

import subprocess
import sys


def test_facade_does_not_eagerly_import_heavy_modules() -> None:
    probe = (
        "import sys\n"
        "import modex_agent.plugins.defaults.capabilities.sandbox as pkg\n"
        "banned = ('decision', 'guard', 'guard_network', 'guard_path',\n"
        "          'guard_pipeline', 'guard_presentation', 'guard_device',\n"
        "          'interceptor', 'runtime', 'shell_plan', 'delegation',\n"
        "          'selection', 'tool_matrix', 'readonly', 'factory',\n"
        "          'container_executor', 'oci_runtime', 'bwrap_runtime',\n"
        "          'seatbelt_runtime')\n"
        "hits = [m for m in sys.modules\n"
        "        if m.startswith(\n"
        "            'modex_agent.plugins.defaults.capabilities.sandbox.')\n"
        "        and any(m.endswith('.' + b) for b in banned)]\n"
        "assert not hits, f'eagerly imported: {hits}'\n"
        "assert 'modex_agent.plugins.defaults.capabilities.approval.factory' not in sys.modules, (\n"
        "    'the facade pulled the approval bundle heavy factory'\n"
        ")\n"
        "print('import-light OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "import-light OK" in result.stdout


def test_registration_entry_imports_without_facade_side_effects() -> None:
    """The registration module stays import-light too — the interceptor
    factory's heavy imports (selection, interceptor, decision) live inside
    ``create``; the config models stop at pydantic-level settings."""
    probe = (
        "import sys\n"
        "from modex_agent.plugins.defaults.capabilities.sandbox import (\n"
        "    register_sandbox_feature,\n"
        ")\n"
        "assert not any(\n"
        "    m.endswith('.decision') or m.endswith('.interceptor')\n"
        "    or m.endswith('.selection') or m.endswith('.runtime')\n"
        "    for m in sys.modules\n"
        "    if m.startswith(\n"
        "        'modex_agent.plugins.defaults.capabilities.sandbox')\n"
        ")\n"
        "print('registration import-light OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "registration import-light OK" in result.stdout
