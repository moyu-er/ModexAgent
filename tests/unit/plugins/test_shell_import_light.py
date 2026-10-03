"""W1-B3 net-new pin: the shell bundle stays sandbox-import-light.

The shell capability classes load at registration/boot for EVERY
deployment — importing the shell facade must not load the sandbox
bundle's substrate modules (``runtime``, ``shell_plan``,
``container_executor``, ``interceptor``, ``decision``). Only the
pydantic-level ``settings`` enum is shared; the sandbox-backed executor
path imports inside ``create()``/``assemble()`` at their use sites.
"""

from __future__ import annotations

import subprocess
import sys

_BANNED_SANDBOX_MODULES = (
    "modex_agent.plugins.defaults.capabilities.sandbox.runtime",
    "modex_agent.plugins.defaults.capabilities.sandbox.shell_plan",
    "modex_agent.plugins.defaults.capabilities.sandbox.container_executor",
    "modex_agent.plugins.defaults.capabilities.sandbox.interceptor",
    "modex_agent.plugins.defaults.capabilities.sandbox.decision",
)


def _assert_probe(script: str, marker: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert marker in result.stdout


def test_shell_facade_does_not_load_sandbox_modules() -> None:
    banned = "\n".join(f"    {name!r}," for name in _BANNED_SANDBOX_MODULES)
    probe = (
        "import sys\n"
        "import modex_agent.plugins.defaults.capabilities.shell\n"
        f"banned = (\n{banned}\n)\n"
        "hits = [m for m in banned if m in sys.modules]\n"
        "assert not hits, f'shell facade loaded sandbox modules: {hits}'\n"
        "print('shell import-light OK')\n"
    )
    _assert_probe(probe, "shell import-light OK")


def test_shell_factory_does_not_load_sandbox_substrate() -> None:
    """The TOOL-slot factory module itself (imported by the facade) keeps
    the substrate imports lazy — the plain subprocess path never loads
    them."""
    banned = "\n".join(f"    {name!r}," for name in _BANNED_SANDBOX_MODULES)
    probe = (
        "import sys\n"
        "from modex_agent.plugins.defaults.capabilities.shell.factory import (\n"
        "    ShellToolGroupFactory,\n"
        ")\n"
        f"banned = (\n{banned}\n)\n"
        "hits = [m for m in banned if m in sys.modules]\n"
        "assert not hits, f'shell factory loaded sandbox modules: {hits}'\n"
        "assert ShellToolGroupFactory is not None\n"
        "print('shell factory import-light OK')\n"
    )
    _assert_probe(probe, "shell factory import-light OK")
