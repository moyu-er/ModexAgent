"""Boundary, observability, and stale-state tests for the judge runner."""

from __future__ import annotations

import logging

import pytest
from bot.eval.judge import runner as runner_module
from bot.eval.judge.rubrics import Rubric, RubricSet, rubric_version
from bot.eval.judge.runner import (
    JudgeConfigurationError,
    JudgeInput,
    JudgeRunner,
    Verdict,
    build_judge_provider_from_env,
)

from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.semconv import GenAiAttr
from modex_agent.trace.store import SpanModel

from .test_runner import RecordingProvider


class CaptureStore(OtelSpanTraceStore):
    """In-memory write capture for the concrete trace-store seam."""

    def __init__(self) -> None:
        self.spans: list[SpanModel] = []

    async def save_span(self, span: SpanModel) -> None:
        self.spans.append(span)


@pytest.fixture
def rubric_set() -> RubricSet:
    return RubricSet(
        name="boundaries",
        rubrics=[
            Rubric(criterion="first", description="Judge the first outcome.", weight=0.6),
            Rubric(criterion="second", description="Judge the second outcome.", weight=0.4),
        ],
    )


def _judge_input(rubric_set: RubricSet) -> JudgeInput:
    return JudgeInput(
        item_context="Evaluate both outcomes.",
        rubric_set=rubric_set,
        agent_output="Both outcomes were attempted.",
        trace_id="candidate-trace",
        session_id="judge-session",
    )


def test_missing_judge_model_fails_without_answer_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: every independent judge environment variable is absent.
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    monkeypatch.delenv("JUDGE_API_KEY", raising=False)
    monkeypatch.delenv("JUDGE_BASE_URL", raising=False)

    # When / Then: provider construction fails clearly at the boundary.
    with pytest.raises(JudgeConfigurationError, match="JUDGE_MODEL"):
        build_judge_provider_from_env()


async def test_env_builder_and_chat_both_pin_temperature_zero(
    monkeypatch: pytest.MonkeyPatch,
    rubric_set: RubricSet,
) -> None:
    # Given: independent judge env values and a constructor-recording replacement.
    provider = RecordingProvider(
        [
            '{"verdicts":['
            '{"criterion":"first","verdict":"MET","evidence":"First done."},'
            '{"criterion":"second","verdict":"MET","evidence":"Second done."}'
            '],"summary":"Both met."}'
        ]
    )
    construction: list[tuple[str, str | None, str | None, float]] = []

    def fake_provider(
        *,
        model: str,
        api_key: str | None,
        base_url: str | None,
        temperature: float,
    ) -> RecordingProvider:
        construction.append((model, api_key, base_url, temperature))
        return provider

    monkeypatch.setenv("JUDGE_MODEL", "judge/env-model")
    monkeypatch.setenv("JUDGE_API_KEY", "judge-key")
    monkeypatch.setenv("JUDGE_BASE_URL", "https://judge.invalid/v1")
    monkeypatch.setattr(runner_module, "LiteLLMProvider", fake_provider)

    # When: the env-built provider executes one judge review.
    built_provider = build_judge_provider_from_env()
    await JudgeRunner(built_provider).review(_judge_input(rubric_set))

    # Then: both construction and the recorded request carry temperature 0.0.
    assert construction == [
        ("judge/env-model", "judge-key", "https://judge.invalid/v1", 0.0)
    ]
    assert provider.temperatures == [0.0]


async def test_unknown_criterion_is_ignored_and_missing_criterion_becomes_na(
    caplog: pytest.LogCaptureFixture,
    rubric_set: RubricSet,
) -> None:
    # Given: valid JSON contains one known and one hallucinated criterion.
    provider = RecordingProvider(
        [
            '{"verdicts":['
            '{"criterion":"first","verdict":"MET","evidence":"First done."},'
            '{"criterion":"invented","verdict":"MET","evidence":"Not in rubric."}'
            '],"summary":"Partial response."}'
        ]
    )

    # When: the tolerant parser reconciles the response to the rubric set.
    with caplog.at_level(logging.WARNING):
        result = await JudgeRunner(provider).review(_judge_input(rubric_set))

    # Then: the unknown entry is warned and omitted; the missing entry is NA.
    assert [(item.criterion, item.verdict) for item in result.verdicts] == [
        ("first", Verdict.MET),
        ("second", Verdict.NA),
    ]
    assert result.na_count == 1
    assert result.weighted_score == pytest.approx(0.6)
    assert "invented" in caplog.text


async def test_review_emits_one_independent_judge_root_span(
    rubric_set: RubricSet,
) -> None:
    # Given: a trace store and a response with one verdict of each judged kind.
    store = CaptureStore()
    provider = RecordingProvider(
        [
            '{"verdicts":['
            '{"criterion":"first","verdict":"MET","evidence":"First done."},'
            '{"criterion":"second","verdict":"UNMET","evidence":"Second missing."}'
            '],"summary":"Mixed."}'
        ]
    )

    # When: the review completes with tracing enabled.
    result = await JudgeRunner(provider, trace_store=store).review(
        _judge_input(rubric_set)
    )

    # Then: exactly one independent root span carries the result distribution.
    assert len(store.spans) == 1
    span = store.spans[0]
    assert span.name == "judge.review"
    assert span.parent_span_id is None
    assert span.trace_id != "candidate-trace"
    assert span.attributes["judge_model"] == "judge/test-model"
    assert span.attributes["rubric_version"] == rubric_version(rubric_set)
    assert span.attributes["verdict_met_count"] == 1
    assert span.attributes["verdict_unmet_count"] == 1
    assert span.attributes["verdict_na_count"] == 0
    assert span.attributes["verdict_cannot_assess_count"] == 0
    assert span.attributes["weighted_score"] == result.weighted_score
    assert span.attributes["candidate_trace_id"] == "candidate-trace"
    assert span.attributes[GenAiAttr.CONVERSATION_ID] == "judge-session"


async def test_two_reviews_do_not_share_verdict_state(rubric_set: RubricSet) -> None:
    # Given: one runner receives two different scripted responses in sequence.
    provider = RecordingProvider(
        [
            '{"verdicts":['
            '{"criterion":"first","verdict":"MET","evidence":"Done."},'
            '{"criterion":"second","verdict":"MET","evidence":"Done."}'
            '],"summary":"First review."}',
            '{"verdicts":['
            '{"criterion":"first","verdict":"UNMET","evidence":"Missing."}'
            '],"summary":"Second review."}',
        ]
    )
    runner = JudgeRunner(provider)

    # When: the same runner instance reviews twice.
    first = await runner.review(_judge_input(rubric_set))
    second = await runner.review(_judge_input(rubric_set))

    # Then: the second result is built only from its own response.
    assert [item.verdict for item in first.verdicts] == [Verdict.MET, Verdict.MET]
    assert [item.verdict for item in second.verdicts] == [Verdict.UNMET, Verdict.NA]
    assert second.summary == "Second review."
    assert len(provider.messages) == 2
