"""WinptyBackend — transport-level umbrella for the Windows winpty family.

ADR-0010 Decision 3: backends are named by transport, not by OS, not by
visibility. The Windows winpty transport has two structural subtypes:

- ``WinptyHiddenBackend``: in-process winpty.
- ``WinptyConsoleWindowBackend``: externally-hosted console window + TCP socket
  bridge (``visible_windows_host.py``).

The umbrella class exists so the factory's capability table and architecture
guards can refer to "the winpty transport" without naming a specific
visibility subclass. It contains no I/O logic — both subclass start() paths
share almost nothing (in-process vs subprocess+socket, see ADR-0010 Decision
4 "structural" branch).
"""

from __future__ import annotations

from modex_agent.tools.terminal.backends.base import TerminalBackend
from modex_agent.tools.terminal.types import Platform


class WinptyBackend(TerminalBackend):
    """Abstract umbrella for the Windows winpty transport.

    Subclasses must set ``visibility`` to either ``VISIBLE`` or ``HIDDEN``.
    """

    platform = Platform.WINDOWS
