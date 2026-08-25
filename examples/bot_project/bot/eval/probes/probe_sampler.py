"""Deterministic fact and expectation construction for probe cases."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import assert_never

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.probes.schema import Fact, Persona, Probe, ProbeType


class ProbeSlot(BaseModel):
    """A probe type's stable position and arm assignment in a library."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_type: ProbeType
    index: int = Field(ge=0)
    dual_arm: bool


class FactSeed(BaseModel):
    """Inputs that distinguish one fact from its probe's shared context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fact_id: str = Field(min_length=1)
    value: str = Field(min_length=1)
    day: int = Field(ge=0)
    superseded_by: str | None = None
    depends_on: list[str] = Field(default_factory=list)


class ProbeAnswer(BaseModel):
    """Expected and forbidden evidence attached to one answer probe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected: list[Fact] = Field(min_length=1)
    forbidden: list[Fact] = Field(default_factory=list)


class ProbeSampler:
    """Construct one orthogonal probe case from a seeded library position."""

    def __init__(
        self,
        rng: random.Random,
        personas: list[Persona],
        start: datetime,
    ) -> None:
        self._rng = rng
        self._personas = personas
        self._start = start

    def sample(self, slot: ProbeSlot) -> tuple[list[Fact], Probe]:
        """Sample the facts and exact expected answer for one probe slot."""
        prefix = f"{'dual' if slot.dual_arm else 'main'}-{slot.probe_type.value}-{slot.index:03d}"
        persona = self._personas[slot.index % len(self._personas)]
        attributes = ("city", "drink", "music", "exercise", "project", "meal")
        attribute = attributes[
            (slot.index + self._rng.randrange(len(attributes))) % len(attributes)
        ]
        values = (
            "Oslo",
            "Lima",
            "sencha",
            "espresso",
            "jazz",
            "ambient",
            "cycling",
            "swimming",
            "Atlas",
            "Beacon",
            "ramen",
            "risotto",
        )
        first_value, second_value = self._rng.sample(values, 2)
        day = self._rng.randrange(0, 35)
        match slot.probe_type:
            case ProbeType.EXTRACTION:
                fact = self._fact(
                    persona,
                    attribute,
                    FactSeed(fact_id=prefix, value=first_value, day=day),
                )
                return [fact], self._probe(persona, slot, ProbeAnswer(expected=[fact]))
            case ProbeType.TEMPORAL:
                old = self._fact(
                    persona,
                    attribute,
                    FactSeed(fact_id=f"{prefix}-old", value=first_value, day=day),
                )
                new = self._fact(
                    persona,
                    attribute,
                    FactSeed(fact_id=f"{prefix}-new", value=second_value, day=day + 7),
                )
                return [old, new], self._probe(
                    persona,
                    slot,
                    ProbeAnswer(expected=[old, new]),
                )
            case ProbeType.KNOWLEDGE_UPDATE:
                old_id = f"{prefix}-old"
                new_id = f"{prefix}-new"
                old = self._fact(
                    persona,
                    attribute,
                    FactSeed(
                        fact_id=old_id,
                        value=first_value,
                        day=day,
                        superseded_by=new_id,
                    ),
                )
                new = self._fact(
                    persona,
                    attribute,
                    FactSeed(
                        fact_id=new_id,
                        value=second_value,
                        day=day + 7,
                        depends_on=[old_id],
                    ),
                )
                return [old, new], self._probe(
                    persona,
                    slot,
                    ProbeAnswer(expected=[new], forbidden=[old]),
                )
            case ProbeType.REFUSAL:
                return [], Probe(
                    probe_id=prefix,
                    probe_type=slot.probe_type,
                    persona_id=persona.persona_id,
                    question=f"What is my unrecorded {attribute}?",
                    expected_answers=[],
                    fact_ids=[],
                    dual_arm=slot.dual_arm,
                )
            case ProbeType.CROSS_USER_ISOLATION:
                other = self._personas[(slot.index + 1) % len(self._personas)]
                own = self._fact(
                    persona,
                    attribute,
                    FactSeed(fact_id=f"{prefix}-own", value=first_value, day=day),
                )
                forbidden = self._fact(
                    other,
                    attribute,
                    FactSeed(fact_id=f"{prefix}-other", value=second_value, day=day),
                )
                return [own, forbidden], self._probe(
                    persona,
                    slot,
                    ProbeAnswer(expected=[own], forbidden=[forbidden]),
                )
            case unreachable:
                assert_never(unreachable)

    def _fact(self, persona: Persona, attribute: str, seed: FactSeed) -> Fact:
        return Fact(
            fact_id=seed.fact_id,
            persona_id=persona.persona_id,
            attribute=attribute,
            value=seed.value,
            valid_from=self._start + timedelta(days=seed.day),
            superseded_by=seed.superseded_by,
            depends_on=seed.depends_on,
            surface_refs=[f"My {attribute} is {seed.value}."],
        )

    def _probe(self, persona: Persona, slot: ProbeSlot, answer: ProbeAnswer) -> Probe:
        return Probe(
            probe_id=(
                f"{'dual' if slot.dual_arm else 'main'}-{slot.probe_type.value}-{slot.index:03d}"
            ),
            probe_type=slot.probe_type,
            persona_id=persona.persona_id,
            question=f"What should you remember about my {answer.expected[-1].attribute}?",
            expected_answers=[fact.value for fact in answer.expected],
            fact_ids=[fact.fact_id for fact in answer.expected],
            forbidden_fact_ids=[fact.fact_id for fact in answer.forbidden],
            dual_arm=slot.dual_arm,
        )
