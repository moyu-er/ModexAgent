"""Zero-producer gate for the unified turn-event stream (W5).

Static AST scan: EVERY core ``TurnEvent`` kind literal must have at least
one construction site of its event class somewhere in production code
(``src/modex_agent`` + ``examples/bot_project/bot``). A union variant
nobody constructs is a dead vocabulary entry — the stream contract says
append-only with real producers, so a producer-less kind fails loudly
listing every offender.

``turn_finished`` counts the ``turn_finished_event`` helper's internal
construction (``core/emitter.py``) — the exactly-once terminal projection
every plane funnels through. Match-pattern references (``case
TurnFinishedEvent():``) do NOT count: they consume, they do not produce.
"""

from __future__ import annotations

import ast
import typing
from pathlib import Path

from modex_agent.core.turn_events import TurnEvent

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "src" / "modex_agent",
    REPO_ROOT / "examples" / "bot_project" / "bot",
)


def _kind_to_class() -> dict[str, str]:
    """Map every ``kind`` literal in the union to its event class name."""
    union = typing.get_args(TurnEvent)[0]
    mapping: dict[str, str] = {}
    for variant in typing.get_args(union):
        kind = typing.get_args(variant.model_fields["kind"].annotation)
        for literal in kind:
            mapping[literal] = variant.__name__
    return mapping


def _construction_sites(class_name: str) -> list[str]:
    """Every ``<class_name>(...)`` call site under the scan roots."""
    sites: list[str] = []
    for root in SCAN_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == class_name
                    or isinstance(func, ast.Attribute)
                    and func.attr == class_name
                ):
                    sites.append(f"{path}:{node.lineno}")
    return sites


def test_every_turn_event_kind_has_a_producer() -> None:
    """Fail loudly listing every kind with no construction site."""
    producerless: list[str] = []
    for kind, class_name in sorted(_kind_to_class().items()):
        if not _construction_sites(class_name):
            producerless.append(f"{kind} ({class_name})")
    assert not producerless, (
        "TurnEvent kinds with zero production construction sites: "
        + ", ".join(producerless)
        + " — every union variant needs a real producer in src/modex_agent "
        "or examples/bot_project/bot"
    )


if __name__ == "__main__":
    from pytest import main

    main([__file__, "-v"])
