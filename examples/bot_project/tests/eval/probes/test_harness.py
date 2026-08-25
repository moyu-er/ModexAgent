from __future__ import annotations

import json
from pathlib import Path

import pytest
from bot.eval.probes.harness import (
    ExperimentAttribute,
    HarnessStatus,
    MemorySnapshot,
    ProbeCheckpoint,
    ProbeItemStatus,
    run_probe_harness,
)

from modex_agent.trace.store import JsonlSpanQuery
from tests.eval.probes.harness_fakes import (
    RecordingScoreInjector,
    ScriptedAnswerProvider,
    harness_config,
    passing_score,
    services,
    write_five_probe_library,
)


class ScriptedScorerError(RuntimeError):
    pass


async def test_scripted_probe_runs_all_four_stages_with_linked_spans_and_scores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")
    library_path, manifest_path, world = write_five_probe_library(tmp_path)
    config = harness_config(tmp_path, library_path, manifest_path)
    provider = ScriptedAnswerProvider()
    injector = RecordingScoreInjector()

    # When
    result = await run_probe_harness(config, services(provider, injector))

    # Then
    assert result.status is HarnessStatus.COMPLETE
    assert result.ingested_turns == sum(len(session.turns) for session in world.sessions)
    assert len(result.records) == 5
    assert provider.questions == [probe.question for probe in world.probes]
    assert provider.tool_arguments == [None] * 5
    snapshot = MemorySnapshot.model_validate_json(config.snapshot_path.read_text(encoding="utf-8"))
    assert snapshot.max_context_tokens == 32_000
    assert snapshot.dream.exhausted is True
    assert {entry.name for persona in snapshot.personas for entry in persona.files} == {
        "MEMORY.md",
        "SOUL.md",
        "USER.md",
    }
    comments = [json.loads(batch[1][0].comment or "") for batch in injector.batches]
    assert all(comment["scorer"] == "verifier" for comment in comments)
    assert all(comment["report_source"] == "counters" for comment in comments)
    assert all(comment["run_ref"] == "experiment-memory-probes" for comment in comments)

    query = JsonlSpanQuery(config.workspace / "trace")
    spans = [
        span
        for probe in world.probes
        for span in await query.list_by_session(f"probe.{probe.probe_id}")
    ]
    linked = [span for span in spans if span.name in {"memory.context.assembled", "probe.answer"}]
    assert len(linked) == 10
    for span in linked:
        assert all(attribute.value in span.attributes for attribute in ExperimentAttribute)
        assert (
            span.attributes[ExperimentAttribute.ITEM_ROOT_OBSERVATION_ID.value]
            == span.span_id
        )


async def test_resume_after_two_of_five_runs_only_remaining_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")
    library_path, manifest_path, _world = write_five_probe_library(tmp_path)
    config = harness_config(tmp_path, library_path, manifest_path)
    calls = 0

    def interrupted_score(record):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise SystemExit("simulated process kill")
        return passing_score(record)

    first_provider = ScriptedAnswerProvider()
    with pytest.raises(SystemExit, match="simulated process kill"):
        await run_probe_harness(
            config,
            services(
                first_provider,
                RecordingScoreInjector(),
                score_fn=interrupted_score,
            ),
        )
    assert len(_checkpoint_lines(config.checkpoint_path)) == 2

    second_provider = ScriptedAnswerProvider()

    # When
    result = await run_probe_harness(
        config,
        services(second_provider, RecordingScoreInjector()),
    )

    # Then
    checkpoints = _checkpoint_lines(config.checkpoint_path)
    assert result.status is HarnessStatus.COMPLETE
    assert len(second_provider.questions) == 3
    assert len(checkpoints) == 5
    assert len({checkpoint.probe_id for checkpoint in checkpoints}) == 5


async def test_low_cost_cap_stops_before_next_call_and_preserves_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")
    library_path, manifest_path, _world = write_five_probe_library(tmp_path)
    config = harness_config(tmp_path, library_path, manifest_path).model_copy(
        update={"max_cost_usd": 0.015, "minimum_call_reserve_usd": 0.006}
    )
    provider = ScriptedAnswerProvider(tokens_per_call=10_000)

    # When
    result = await run_probe_harness(
        config,
        services(provider, RecordingScoreInjector()),
    )

    # Then
    assert result.status is HarnessStatus.COST_CAPPED
    assert len(provider.questions) == 1
    assert len(result.records) == 1
    assert len(_checkpoint_lines(config.checkpoint_path)) == 1
    assert result.spent_cost_usd == pytest.approx(0.01)


async def test_single_probe_failure_is_checkpointed_and_does_not_block_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")
    library_path, manifest_path, world = write_five_probe_library(tmp_path)
    config = harness_config(tmp_path, library_path, manifest_path)

    def score_with_one_failure(record):
        if record.probe.probe_id == world.probes[1].probe_id:
            raise ScriptedScorerError("scripted scorer failure")
        return passing_score(record)

    provider = ScriptedAnswerProvider()

    # When
    result = await run_probe_harness(
        config,
        services(
            provider,
            RecordingScoreInjector(),
            score_fn=score_with_one_failure,
        ),
    )

    # Then
    assert result.status is HarnessStatus.COMPLETE
    assert len(provider.questions) == 5
    assert len(result.records) == 4
    assert len(result.failures) == 1
    assert result.failures[0].status is ProbeItemStatus.FAILED
    assert "scripted scorer failure" in (result.failures[0].error or "")
    assert len(_checkpoint_lines(config.checkpoint_path)) == 5


def _checkpoint_lines(path: Path) -> list[ProbeCheckpoint]:
    return [
        ProbeCheckpoint.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]
