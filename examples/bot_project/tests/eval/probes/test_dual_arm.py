from __future__ import annotations

from datetime import UTC, datetime

import pytest
from bot.eval.memory_metric_models import UtilizationClass
from bot.eval.probes._harness_models import ProbeRunRecord
from bot.eval.probes.dual_arm import (
    NomemoryRunConfig,
    NomemoryRunServices,
    classify_utilization,
    run_nomemory_controls,
)
from bot.eval.probes.sampler import LibraryScale, config_for_scale, sample_world
from bot.eval.probes.schema import Probe

from tests.eval.probes.harness_fakes import (
    ScriptedAnswerProvider,
    passing_score,
    pricebook,
)


@pytest.mark.parametrize(
    ("memory_passed", "nomemory_passed", "expected"),
    [
        (True, False, UtilizationClass.BENEFICIAL),
        (False, True, UtilizationClass.HARMFUL),
        (False, False, UtilizationClass.IGNORED),
        (True, True, UtilizationClass.NEUTRAL),
    ],
)
def test_classify_utilization_maps_the_four_dual_arm_outcomes(
    memory_passed: bool,
    nomemory_passed: bool,
    expected: UtilizationClass,
) -> None:
    # Given / When
    result = classify_utilization(memory_passed, nomemory_passed)

    # Then
    assert result is expected


async def test_nomemory_controls_query_only_dual_arm_probes_with_empty_context() -> None:
    # Given
    world = sample_world(seed=21, config=config_for_scale(LibraryScale.SMOKE))
    memory_records = [_record(probe) for probe in world.probes]
    provider = ScriptedAnswerProvider()

    # When
    result = await run_nomemory_controls(
        memory_records,
        NomemoryRunConfig(
            max_cost_usd=1.0,
            minimum_call_reserve_usd=0.001,
            answer_max_output_tokens=100,
        ),
        NomemoryRunServices(
            provider=provider,
            pricebook=pricebook(),
            score_fn=passing_score,
        ),
    )

    # Then
    dual_probes = [probe for probe in world.probes if probe.dual_arm]
    assert provider.questions == [probe.question for probe in dual_probes]
    assert provider.tool_arguments == [None] * len(dual_probes)
    assert [item.record.assembled_context for item in result.results] == [""] * len(dual_probes)
    assert result.cost_capped is False
    assert result.spent_cost_usd > 0


async def test_nomemory_controls_stop_before_an_unaffordable_second_arm_call() -> None:
    # Given
    world = sample_world(seed=21, config=config_for_scale(LibraryScale.SMOKE))
    records = [_record(probe) for probe in world.probes if probe.dual_arm]
    provider = ScriptedAnswerProvider(tokens_per_call=100)

    # When
    result = await run_nomemory_controls(
        records,
        NomemoryRunConfig(
            max_cost_usd=0.00015,
            minimum_call_reserve_usd=0.0001,
            answer_max_output_tokens=100,
        ),
        NomemoryRunServices(
            provider=provider,
            pricebook=pricebook(),
            score_fn=passing_score,
        ),
    )

    # Then
    assert result.cost_capped is True
    assert len(result.results) == 1
    assert len(provider.questions) == 1
    assert result.spent_cost_usd == pytest.approx(0.0001)


def _record(probe: Probe) -> ProbeRunRecord:
    now = datetime.now(UTC)
    return ProbeRunRecord(
        probe=probe,
        answer="memory answer",
        assembled_context="memory context",
        trace_id=f"trace-{probe.probe_id}",
        span_id=f"span-{probe.probe_id}",
        started_at=now,
        completed_at=now,
        snapshot_captured_at=now,
    )
