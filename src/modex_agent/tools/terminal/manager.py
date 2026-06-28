"""TerminalManager — deprecated alias for BaseTerminalManager (ADR-0010 Decision 8).

History: this file previously held a second ``TerminalManager`` class adding
LRU eviction, JSON persistence, and memory-pressure buffer clearing on top
of ``BaseTerminalManager``. ADR-0010 Decision 8 folds those capabilities inward
— they are now flag-guarded private methods on ``BaseTerminalManager``
(``max_terminals`` / ``storage_dir`` / ``enable_memory_pressure`` constructor
parameters, all default-off).

This file retains a deprecated re-export ``TerminalManager = BaseTerminalManager``
for a one-to-two release migration window because the e2e verification tests
under ``tests/verify_terminal_e2e_*.py`` instantiate ``TerminalManager`` by name.
NOTE: the alias does NOT preserve the legacy ``TerminalManager.__init__`` signature;
those verify-tests are migrated in Phase 6.
"""

from __future__ import annotations

from modex_agent.tools.terminal.managers import BaseTerminalManager

# Deprecated alias — kept for the test migration window only.
# New callers MUST construct BaseTerminalManager directly with the capability
# flags they need.
TerminalManager = BaseTerminalManager

__all__ = ["TerminalManager"]
