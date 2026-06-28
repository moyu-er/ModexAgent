"""Architecture guard: TerminalManager's persistence/eviction capability is
retained even though the original second class has been folded into
``BaseTerminalManager`` (ADR-0010 Decision 8, closing the ADR-0007 fork).

A future review that sees the capability helpers as "unused" and proposes
deletion must fail this test. Under ADR-0010 these helpers live on
``BaseTerminalManager`` itself, guarded by capability flags, rather than on a
second class.
"""

from __future__ import annotations

from modex_agent.tools.terminal.managers import BaseTerminalManager, TerminalManagerBase

_CAPABILITY_METHODS = (
    "save_state",
    "load_state",
    "_evict_oldest",
    "_check_memory_pressure",
)


def test_base_terminal_manager_retains_capability_methods() -> None:
    missing = [m for m in _CAPABILITY_METHODS if not hasattr(BaseTerminalManager, m)]
    assert not missing, (
        f"BaseTerminalManager lost capability methods {missing}. Per ADR-0007 + "
        "ADR-0010 Decision 8 these are real folded-in implementations, not stubs."
    )


def test_terminal_manager_base_abc_still_exists() -> None:
    """The seam ABC still exists with at least one production subclass."""
    assert issubclass(BaseTerminalManager, TerminalManagerBase)


def test_capability_method_list_is_nonempty() -> None:
    """Sanity: the guard must actually watch something."""
    assert _CAPABILITY_METHODS
