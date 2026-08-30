from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from bot.eval.probes.schema import (
    Fact,
    Persona,
    Probe,
    ProbeType,
    Session,
    SessionTurn,
    Speaker,
    WorldSpec,
)
from pydantic import ValidationError

_START = datetime(2026, 1, 5, tzinfo=UTC)


def _fact(
    fact_id: str,
    persona_id: str,
    attribute: str,
    value: str,
    *,
    day: int = 0,
    superseded_by: str | None = None,
    depends_on: list[str] | None = None,
) -> Fact:
    return Fact(
        fact_id=fact_id,
        persona_id=persona_id,
        attribute=attribute,
        value=value,
        valid_from=_START + timedelta(days=day),
        superseded_by=superseded_by,
        depends_on=depends_on or [],
        surface_refs=[f"{attribute} is {value}"],
    )


def _world(probe: Probe, facts: list[Fact]) -> WorldSpec:
    personas = [
        Persona(persona_id="p-a", display_name="Ari", traits=["concise"]),
        Persona(persona_id="p-b", display_name="Bo", traits=["patient"]),
    ]
    return WorldSpec(
        suite_version="v1",
        seed=7,
        personas=personas,
        facts=facts,
        sessions=[
            Session(
                session_id="s-1",
                persona_id="p-a",
                timestamp=_START,
                turns=[
                    SessionTurn(
                        speaker=Speaker.USER,
                        text="A factual sentence",
                        fact_ids=[fact.fact_id for fact in facts if fact.persona_id == "p-a"],
                    )
                ],
            )
        ],
        probes=[probe],
    )


@pytest.mark.parametrize(
    ("probe", "facts"),
    [
        (
            Probe(
                probe_id="extract",
                probe_type=ProbeType.EXTRACTION,
                persona_id="p-a",
                question="What city do I prefer?",
                expected_answers=["Oslo"],
                fact_ids=["f-city"],
            ),
            [_fact("f-city", "p-a", "city", "Oslo")],
        ),
        (
            Probe(
                probe_id="temporal",
                probe_type=ProbeType.TEMPORAL,
                persona_id="p-a",
                question="Which cities did I prefer in order?",
                expected_answers=["Oslo", "Lima"],
                fact_ids=["f-city-old", "f-city-new"],
            ),
            [
                _fact("f-city-old", "p-a", "city", "Oslo", day=1),
                _fact("f-city-new", "p-a", "city", "Lima", day=9),
            ],
        ),
        (
            Probe(
                probe_id="update",
                probe_type=ProbeType.KNOWLEDGE_UPDATE,
                persona_id="p-a",
                question="What city do I prefer now?",
                expected_answers=["Lima"],
                fact_ids=["f-city-new"],
                forbidden_fact_ids=["f-city-old"],
            ),
            [
                _fact(
                    "f-city-old",
                    "p-a",
                    "city",
                    "Oslo",
                    day=1,
                    superseded_by="f-city-new",
                ),
                _fact(
                    "f-city-new",
                    "p-a",
                    "city",
                    "Lima",
                    day=9,
                    depends_on=["f-city-old"],
                ),
            ],
        ),
        (
            Probe(
                probe_id="refusal",
                probe_type=ProbeType.REFUSAL,
                persona_id="p-a",
                question="What is my passport number?",
                expected_answers=[],
                fact_ids=[],
            ),
            [],
        ),
        (
            Probe(
                probe_id="isolation",
                probe_type=ProbeType.CROSS_USER_ISOLATION,
                persona_id="p-b",
                question="Which city do I prefer?",
                expected_answers=["Lima"],
                fact_ids=["f-b-city"],
                forbidden_fact_ids=["f-a-city"],
            ),
            [
                _fact("f-a-city", "p-a", "city", "Oslo"),
                _fact("f-b-city", "p-b", "city", "Lima"),
            ],
        ),
    ],
)
def test_world_accepts_known_outcome_for_each_orthogonal_probe_type(
    probe: Probe,
    facts: list[Fact],
) -> None:
    world = _world(probe, facts)

    assert world.probes == [probe]


def test_world_rejects_refusal_with_grounded_fact() -> None:
    probe = Probe(
        probe_id="bad-refusal",
        probe_type=ProbeType.REFUSAL,
        persona_id="p-a",
        question="Unknown?",
        expected_answers=[],
        fact_ids=["f-city"],
    )

    with pytest.raises(ValidationError, match="refusal"):
        _world(probe, [_fact("f-city", "p-a", "city", "Oslo")])


def test_world_rejects_update_without_supersession_edge() -> None:
    probe = Probe(
        probe_id="bad-update",
        probe_type=ProbeType.KNOWLEDGE_UPDATE,
        persona_id="p-a",
        question="Current city?",
        expected_answers=["Lima"],
        fact_ids=["new"],
        forbidden_fact_ids=["old"],
    )

    with pytest.raises(ValidationError, match="supersession"):
        _world(
            probe,
            [
                _fact("old", "p-a", "city", "Oslo"),
                _fact("new", "p-a", "city", "Lima", day=1),
            ],
        )


def test_world_rejects_isolation_without_cross_user_evidence() -> None:
    probe = Probe(
        probe_id="bad-isolation",
        probe_type=ProbeType.CROSS_USER_ISOLATION,
        persona_id="p-a",
        question="City?",
        expected_answers=["Oslo"],
        fact_ids=["own"],
        forbidden_fact_ids=["also-own"],
    )

    with pytest.raises(ValidationError, match="cross-user"):
        _world(
            probe,
            [
                _fact("own", "p-a", "city", "Oslo"),
                _fact("also-own", "p-a", "city", "Lima"),
            ],
        )


def test_world_rejects_unknown_fact_annotation() -> None:
    probe = Probe(
        probe_id="unknown",
        probe_type=ProbeType.EXTRACTION,
        persona_id="p-a",
        question="City?",
        expected_answers=["Oslo"],
        fact_ids=["missing"],
    )

    with pytest.raises(ValidationError, match="missing"):
        _world(probe, [])


def test_schema_is_frozen_and_forbids_extra_fields() -> None:
    fact = _fact("f-city", "p-a", "city", "Oslo")

    with pytest.raises(ValidationError):
        Fact.model_validate({**fact.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        fact.value = "Lima"
