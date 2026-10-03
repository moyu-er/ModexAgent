"""W1-B2 net-new pin: the approval bundle's import-light facade.

Importing the registration entry must NOT eagerly import the bundle's
sandbox-coupled implementation modules (``security``, ``resumer``,
``factory``) nor ``modex_agent.plugins.defaults.capabilities.sandbox.decision`` — the framework import
graph only loads bundle implementation modules when approval is actually
in play. Pinned via ``sys.modules`` in a subprocess (the same pattern as
the experience bundle's facade test).
"""

from __future__ import annotations

import subprocess
import sys


def test_facade_does_not_eagerly_import_heavy_modules() -> None:
    probe = (
        "import sys\n"
        "import modex_agent.plugins.defaults.capabilities.approval as pkg\n"
        "banned = ('security', 'resumer', 'factory', 'ui', 'renderer')\n"
        "hits = [m for m in sys.modules\n"
        "        if m.startswith(\n"
        "            'modex_agent.plugins.defaults.capabilities.approval.')\n"
        "        and any(m.endswith('.' + b) for b in banned)]\n"
        "assert not hits, f'eagerly imported: {hits}'\n"
        "assert 'modex_agent.plugins.defaults.capabilities.sandbox.decision' not in sys.modules, (\n"
        "    'the facade pulled the sandbox decision service'\n"
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
    """The registration module stays import-light too (the stage/command
    factories construct without heavy imports; the gate factory registers
    nothing — its consumers import it inside function bodies)."""
    probe = (
        "import sys\n"
        "from modex_agent.plugins.defaults.capabilities.approval import (\n"
        "    register_approval_feature,\n"
        ")\n"
        "assert not any(\n"
        "    m.endswith('.security') or m.endswith('.resumer')\n"
        "    or m.endswith('.factory')\n"
        "    for m in sys.modules\n"
        "    if m.startswith(\n"
        "        'modex_agent.plugins.defaults.capabilities.approval')\n"
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
