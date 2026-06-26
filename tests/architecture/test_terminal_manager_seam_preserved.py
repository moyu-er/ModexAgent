"""Architecture guard: TerminalManager's persistence/eviction capability is
retained even though it currently has zero production callers (ADR-0007).

A future review that sees TerminalManager as "unused" and proposes deletion
must fail this test and justify itself against ADR-0007 — the capability
(save_state / load_state / _evict_oldest / _check_memory_pressure) is a
designed seam, not dead code. This is the semantic inverse of
test_dead_code_gone.py: that guards dead symbols stay gone; this guards a
live capability stays present.

See candidate-⑤ spec Part B (docs/refactor/candidate-5-tools-sandbox.md).
"""
from __future__ import annotations

from modex_agent.tools.terminal.manager import TerminalManager

# Capability markers that distinguish TerminalManager from the lean
# BaseTerminalManager. If any are removed, the persistence/eviction seam is
# being deleted — do not let that happen silently.
_CAPABILITY_METHODS = (
    "save_state",
    "load_state",
    "_evict_oldest",
    "_check_memory_pressure",
)


def test_terminal_manager_retains_persistence_capability() -> None:
    missing = [m for m in _CAPABILITY_METHODS if not hasattr(TerminalManager, m)]
    assert not missing, (
        f"TerminalManager lost capability methods {missing}. Per ADR-0007 this "
        "is a real seam retained at zero callers — do not delete as 'unused'. "
        "Folding inward onto BaseTerminalManager is a separate decision; see "
        "candidate-⑤ spec Part B."
    )


def test_capability_method_list_is_nonempty() -> None:
    """Sanity: the guard must actually watch something."""
    assert _CAPABILITY_METHODS
