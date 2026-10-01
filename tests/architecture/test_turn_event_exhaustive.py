"""Exhaustiveness anchors for the unified turn-event stream (ADR-0054).

Mechanical guards over the closed core ``TurnEvent`` union and the
migration translator:

A. **Projector disposition completeness** — every core ``TurnEvent`` kind
   is either mapped (``MAPPED_TURN_EVENT_KINDS``) or declared ignored
   (``IGNORED_TURN_EVENT_KINDS``). A new union variant without a declared
   disposition fails loudly (no silent drops).
B. **Translator table completeness** — the bot translator's declarative
   ``_REACT_TO_TURN_KINDS`` table covers EVERY ``ReActEvent`` value of
   the runtime enum; each entry is a core kind literal or ``None``
   (declared ignored).
C. **Translator table validity** — every non-``None`` entry of the table
   is a real ``TurnEvent`` kind literal (no typos).

The bot table is read by AST (framework tests must not import example
code); the enum and the union are imported from the framework.
"""

from __future__ import annotations

import ast
import typing
from pathlib import Path

from modex_agent.agents.react.agent import ReActEvent
from modex_agent.core.turn_events import TurnEvent
from modex_agent.presentation.projector import DefaultTurnEventProjector

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_TRANSLATOR_PATH = (
    REPO_ROOT / "examples" / "bot_project" / "bot" / "webui" / "emitter" / "bot_transcript.py"
)
TABLE_NAME = "_REACT_TO_TURN_KINDS"


def _turn_event_kinds() -> set[str]:
    """Every ``kind`` literal in the core ``TurnEvent`` union."""
    union = typing.get_args(TurnEvent)[0]
    kinds: set[str] = set()
    for variant in typing.get_args(union):
        kinds.update(typing.get_args(variant.model_fields["kind"].annotation))
    return kinds


def _parse_bot_table() -> dict[str, str | None]:
    """Read ``_REACT_TO_TURN_KINDS`` from the bot translator via AST."""
    tree = ast.parse(BOT_TRANSLATOR_PATH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id != TABLE_NAME:
                continue
            table: dict[str, str | None] = {}
            for key, value in zip(node.value.keys, node.value.values, strict=True):  # type: ignore[union-attr]
                # Keys are ``ReActEvent.<MEMBER>.value`` attribute chains;
                # values are string literals or the ``None`` constant.
                member = key.value.attr  # type: ignore[union-attr]
                entry = value.value  # type: ignore[union-attr]
                assert isinstance(entry, str | None), f"non-literal entry for {member}"
                table[getattr(ReActEvent, member).value] = entry
            return table
    raise AssertionError(f"{TABLE_NAME} not found in {BOT_TRANSLATOR_PATH}")


def test_projector_disposition_covers_every_turn_event_kind() -> None:
    """Anchor A: mapped ∪ ignored == the full core kind set."""
    mapped = set(DefaultTurnEventProjector.MAPPED_TURN_EVENT_KINDS)
    ignored = set(DefaultTurnEventProjector.IGNORED_TURN_EVENT_KINDS)
    all_kinds = _turn_event_kinds()
    missing = all_kinds - (mapped | ignored)
    assert not missing, f"TurnEvent kinds without a declared disposition: {missing}"
    extra = (mapped | ignored) - all_kinds
    assert not extra, f"disposition names kinds that are not in the union: {extra}"
    assert not (mapped & ignored), "a kind cannot be both mapped and ignored"


def test_bot_translator_table_covers_every_react_event_value() -> None:
    """Anchor B: every ReActEvent value has a declared disposition."""
    table = _parse_bot_table()
    enum_values = {event.value for event in ReActEvent}
    missing = enum_values - set(table)
    assert not missing, f"ReActEvent values missing from {TABLE_NAME}: {missing}"
    extra = set(table) - enum_values
    assert not extra, f"{TABLE_NAME} names values that are not ReActEvent members: {extra}"


def test_bot_translator_table_entries_are_valid_turn_event_kinds() -> None:
    """Anchor C: non-None table entries are real TurnEvent kind literals."""
    table = _parse_bot_table()
    kinds = _turn_event_kinds()
    invalid = {
        value for value in table.values() if value is not None and value not in kinds
    }
    assert not invalid, f"{TABLE_NAME} entries that are not TurnEvent kinds: {invalid}"


if __name__ == "__main__":
    from pytest import main

    main([__file__, "-v"])
