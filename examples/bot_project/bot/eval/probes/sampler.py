"""Deterministic programmatic worlds and truths for the memory probe suite."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.probes.probe_sampler import ProbeSampler, ProbeSlot
from bot.eval.probes.schema import (
    Persona,
    ProbeType,
    WorldSpec,
)
from bot.eval.probes.timeline import TimelineParameters, TimelineSampler

_START: Final = datetime(2026, 1, 5, tzinfo=UTC)
_NAMES: Final = ("Ari", "Bo", "Cyra", "Dev", "Emi", "Finn", "Gia", "Hale")
_TRAITS: Final = ("concise", "patient", "curious", "methodical", "warm", "direct")


class LibraryScale(StrEnum):
    """Supported committed-library sizes."""

    SMOKE = "smoke"
    FULL = "full"


class SamplerConfig(BaseModel):
    """All scale parameters required to reproduce a sampled library."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    persona_count: int = Field(ge=2, le=len(_NAMES))
    sessions_per_persona: int = Field(ge=1)
    min_tokens_per_persona: int = Field(ge=1)
    max_tokens_per_persona: int = Field(ge=1)
    main_probes_per_type: int = Field(ge=1)
    dual_arm_count: int = Field(ge=0)
    weeks: int = Field(ge=1)


def config_for_scale(scale: LibraryScale) -> SamplerConfig:
    """Return the exact parameters for smoke or frozen-v1 full generation."""
    match scale:
        case LibraryScale.SMOKE:
            return SamplerConfig(
                persona_count=4,
                sessions_per_persona=2,
                min_tokens_per_persona=100,
                max_tokens_per_persona=160,
                main_probes_per_type=1,
                dual_arm_count=2,
                weeks=8,
            )
        case LibraryScale.FULL:
            return SamplerConfig(
                persona_count=8,
                sessions_per_persona=35,
                min_tokens_per_persona=50_000,
                max_tokens_per_persona=80_000,
                main_probes_per_type=25,
                dual_arm_count=30,
                weeks=8,
            )
        case unreachable:
            assert_never(unreachable)


def sample_world(*, seed: int, config: SamplerConfig) -> WorldSpec:
    """Sample personas, timestamps, fact graph, and exact answers without an LLM."""
    rng = random.Random(seed)
    names = rng.sample(_NAMES, config.persona_count)
    personas = [
        Persona(
            persona_id=f"persona-{index + 1:02d}",
            display_name=name,
            traits=rng.sample(_TRAITS, 2),
        )
        for index, name in enumerate(names)
    ]
    facts = []
    probes = []
    probe_sampler = ProbeSampler(rng, personas, _START)
    for probe_type in ProbeType:
        for index in range(config.main_probes_per_type):
            created_facts, probe = probe_sampler.sample(
                ProbeSlot(probe_type=probe_type, index=index, dual_arm=False)
            )
            facts.extend(created_facts)
            probes.append(probe)
    for index in range(config.dual_arm_count):
        probe_type = ProbeType.EXTRACTION if index % 2 == 0 else ProbeType.KNOWLEDGE_UPDATE
        created_facts, probe = probe_sampler.sample(
            ProbeSlot(
                probe_type=probe_type,
                index=config.main_probes_per_type + index,
                dual_arm=True,
            )
        )
        facts.extend(created_facts)
        probes.append(probe)
    sessions = TimelineSampler(
        rng,
        TimelineParameters(
            sessions_per_persona=config.sessions_per_persona,
            min_tokens_per_persona=config.min_tokens_per_persona,
            max_tokens_per_persona=config.max_tokens_per_persona,
            weeks=config.weeks,
        ),
        _START,
    ).sample(personas, facts)
    return WorldSpec(
        suite_version="v1",
        seed=seed,
        personas=personas,
        facts=facts,
        sessions=sessions,
        probes=probes,
    )
