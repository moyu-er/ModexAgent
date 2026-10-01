"""Exhaustiveness anchors for the unified turn-event stream (ADR-0054).

Mechanical guard over the closed core ``TurnEvent`` union:

A. **Projector disposition completeness** — every core ``TurnEvent`` kind
   is either mapped (``MAPPED_TURN_EVENT_KINDS``) or declared ignored
   (``IGNORED_TURN_EVENT_KINDS``). A new union variant without a declared
   disposition fails loudly (no silent drops).

The former enum-translation anchors (the bot ``_REACT_TO_TURN_KINDS``
table over the runtime ``ReActEvent`` enum) died with the enum: the W2
cutover deleted both the enum and the table — the native runtime now
constructs core ``TurnEvent`` objects directly at every emission site.
"""

from __future__ import annotations

from modex_agent.core.turn_events import turn_event_kind_literals
from modex_agent.presentation.projector import DefaultTurnEventProjector


def test_projector_disposition_covers_every_turn_event_kind() -> None:
    """Anchor A: mapped ∪ ignored == the full core kind set."""
    mapped = set(DefaultTurnEventProjector.MAPPED_TURN_EVENT_KINDS)
    ignored = set(DefaultTurnEventProjector.IGNORED_TURN_EVENT_KINDS)
    all_kinds = set(turn_event_kind_literals())
    missing = all_kinds - (mapped | ignored)
    assert not missing, f"TurnEvent kinds without a declared disposition: {missing}"
    extra = (mapped | ignored) - all_kinds
    assert not extra, f"disposition names kinds that are not in the union: {extra}"
    assert not (mapped & ignored), "a kind cannot be both mapped and ignored"


if __name__ == "__main__":
    from pytest import main

    main([__file__, "-v"])
