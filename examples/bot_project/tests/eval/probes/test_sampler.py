from __future__ import annotations

from collections import Counter
from typing import assert_never

from bot.eval.probes.sampler import LibraryScale, config_for_scale, sample_world
from bot.eval.probes.schema import ProbeType


def test_full_scale_matches_frozen_library_resolution() -> None:
    config = config_for_scale(LibraryScale.FULL)

    assert config.persona_count == 8
    assert config.sessions_per_persona == 35
    assert (config.min_tokens_per_persona, config.max_tokens_per_persona) == (50_000, 80_000)
    assert config.main_probes_per_type == 25
    assert config.dual_arm_count == 30
    assert config.weeks == 8


def test_smoke_scale_has_one_main_probe_per_type_and_two_dual_arm_probes() -> None:
    world = sample_world(seed=19, config=config_for_scale(LibraryScale.SMOKE))

    main_counts = Counter(probe.probe_type for probe in world.probes if not probe.dual_arm)
    assert main_counts == Counter(dict.fromkeys(ProbeType, 1))
    assert sum(probe.dual_arm for probe in world.probes) == 2


def test_same_seed_reproduces_identical_world_bytes() -> None:
    config = config_for_scale(LibraryScale.SMOKE)

    first = sample_world(seed=731, config=config).model_dump_json()
    second = sample_world(seed=731, config=config).model_dump_json()

    assert first.encode() == second.encode()


def test_different_seed_changes_personas_timeline_or_values() -> None:
    config = config_for_scale(LibraryScale.SMOKE)

    first = sample_world(seed=1, config=config).model_dump_json()
    second = sample_world(seed=2, config=config).model_dump_json()

    assert first != second


def test_sampler_builds_deterministic_timestamps_personas_and_fact_dependencies() -> None:
    world = sample_world(seed=41, config=config_for_scale(LibraryScale.SMOKE))

    timestamps = [session.timestamp for session in world.sessions]
    assert timestamps == sorted(timestamps)
    assert len({persona.persona_id for persona in world.personas}) == len(world.personas)
    updates = [probe for probe in world.probes if probe.probe_type is ProbeType.KNOWLEDGE_UPDATE]
    assert updates
    for probe in updates:
        new_fact = world.fact_by_id(probe.fact_ids[0])
        old_fact = world.fact_by_id(probe.forbidden_fact_ids[0])
        assert old_fact.superseded_by == new_fact.fact_id
        assert old_fact.fact_id in new_fact.depends_on
        assert old_fact.valid_from < new_fact.valid_from


def test_sampler_materializes_each_persona_token_target_as_deterministic_filler() -> None:
    config = config_for_scale(LibraryScale.SMOKE)
    world = sample_world(seed=47, config=config)

    for persona in world.personas:
        sessions = [
            session for session in world.sessions if session.persona_id == persona.persona_id
        ]
        declared = sum(session.target_tokens for session in sessions)
        filler_tokens = sum(
            turn.text.count("neutral")
            for session in sessions
            for turn in session.turns
            if not turn.fact_ids
        )
        assert config.min_tokens_per_persona <= declared <= config.max_tokens_per_persona
        assert filler_tokens == declared


def test_sampler_truth_is_derived_from_fact_values() -> None:
    world = sample_world(seed=53, config=config_for_scale(LibraryScale.SMOKE))

    for probe in world.probes:
        expected = [world.fact_by_id(fact_id).value for fact_id in probe.fact_ids]
        match probe.probe_type:
            case ProbeType.REFUSAL:
                assert probe.expected_answers == []
            case (
                ProbeType.EXTRACTION
                | ProbeType.TEMPORAL
                | ProbeType.KNOWLEDGE_UPDATE
                | ProbeType.CROSS_USER_ISOLATION
            ):
                assert probe.expected_answers == expected
            case unreachable:
                assert_never(unreachable)
