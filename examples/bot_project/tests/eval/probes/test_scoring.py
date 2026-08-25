from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from bot.eval.probes.schema import Fact, ProbeType
from bot.eval.probes.scoring import (
    ProbeAnswer,
    ProbeScore,
    score_probe,
    summarize_probe_scores,
)
from pydantic import ValidationError

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _fact(
    fact_id: str,
    value: str,
    *,
    day: int = 0,
    superseded_by: str | None = None,
    surface_ref: str | None = None,
) -> Fact:
    return Fact(
        fact_id=fact_id,
        persona_id="persona-b",
        attribute="city",
        value=value,
        valid_from=_START + timedelta(days=day),
        superseded_by=superseded_by,
        surface_refs=[surface_ref or f"My city is {value}."],
    )


def _probe_answer(
    probe_type: ProbeType,
    *,
    answer: str | None,
    context: str | None,
    expected: list[Fact] | None = None,
    forbidden: list[Fact] | None = None,
) -> ProbeAnswer:
    return ProbeAnswer(
        probe_id=f"probe-{probe_type.value}",
        probe_type=probe_type,
        answer=answer,
        injected_context=context,
        expected_evidence=expected or [],
        forbidden_evidence=forbidden or [],
    )


@pytest.mark.parametrize(
    ("probe_answer", "expected_passed"),
    [
        pytest.param(
            _probe_answer(
                ProbeType.EXTRACTION,
                answer="sencha",
                context="The memory says: my   drink is sencha。",
                expected=[
                    _fact("drink", "SENCHA", surface_ref="My drink is SENCHA!"),
                ],
            ),
            True,
            id="normalized-case-whitespace-punctuation-pass",
        ),
        pytest.param(
            _probe_answer(
                ProbeType.EXTRACTION,
                answer="Oslo",
                context="No relevant memory was injected.",
                expected=[_fact("city", "Oslo")],
            ),
            False,
            id="missing-evidence-fail",
        ),
        pytest.param(
            _probe_answer(
                ProbeType.EXTRACTION,
                answer="Oslo",
                context=None,
                expected=[_fact("city", "Oslo")],
            ),
            None,
            id="missing-context-na",
        ),
    ],
)
def test_extraction_known_outcomes(
    probe_answer: ProbeAnswer,
    expected_passed: bool | None,
) -> None:
    assert score_probe(probe_answer).passed is expected_passed


@pytest.mark.parametrize(
    ("answer", "expected_passed"),
    [
        pytest.param("First Oslo, then Lima.", True, id="ordered-pass"),
        pytest.param("First Lima, then Oslo.", False, id="reversed-fail"),
        pytest.param(None, None, id="missing-answer-na"),
    ],
)
def test_temporal_known_outcomes(answer: str | None, expected_passed: bool | None) -> None:
    probe_answer = _probe_answer(
        ProbeType.TEMPORAL,
        answer=answer,
        context="timeline",
        expected=[_fact("new", "Lima", day=8), _fact("old", "Oslo", day=1)],
    )

    assert score_probe(probe_answer).passed is expected_passed


@pytest.mark.parametrize(
    ("answer", "expected_passed"),
    [
        pytest.param("The current city is Lima.", True, id="new-only-pass"),
        pytest.param("The city changed from Oslo to Lima.", False, id="old-value-fail"),
        pytest.param(None, None, id="missing-answer-na"),
    ],
)
def test_knowledge_update_known_outcomes(
    answer: str | None,
    expected_passed: bool | None,
) -> None:
    current = _fact("new", "Lima", day=8)
    probe_answer = _probe_answer(
        ProbeType.KNOWLEDGE_UPDATE,
        answer=answer,
        context="update",
        expected=[current],
        forbidden=[_fact("old", "Oslo", day=1, superseded_by=current.fact_id)],
    )

    assert score_probe(probe_answer).passed is expected_passed


@pytest.mark.parametrize(
    ("answer", "expected_passed"),
    [
        pytest.param("这个信息未提供，我不知道。", True, id="chinese-refusal-pass"),
        pytest.param("I do not know; it was not provided.", True, id="english-refusal-pass"),
        pytest.param("[REFUSAL]", True, id="explicit-marker-pass"),
        pytest.param("Your city is Oslo.", False, id="invented-answer-fail"),
        pytest.param(None, None, id="missing-answer-na"),
    ],
)
def test_refusal_known_outcomes(answer: str | None, expected_passed: bool | None) -> None:
    probe_answer = _probe_answer(
        ProbeType.REFUSAL,
        answer=answer,
        context="No stored truth.",
    )

    assert score_probe(probe_answer).passed is expected_passed


@pytest.mark.parametrize(
    ("answer", "context", "expected_passed"),
    [
        pytest.param(
            "Answer-layer Oslo is judged elsewhere.",
            "B's own city is Lima.",
            True,
            id="clean-context-pass-despite-answer-leak",
        ),
        pytest.param(
            "Lima",
            "Cross-user memory: MY CITY IS OSLO！",
            False,
            id="contaminated-context-fail",
        ),
        pytest.param("Lima", None, None, id="missing-context-na"),
    ],
)
def test_isolation_known_outcomes(
    answer: str,
    context: str | None,
    expected_passed: bool | None,
) -> None:
    probe_answer = _probe_answer(
        ProbeType.CROSS_USER_ISOLATION,
        answer=answer,
        context=context,
        expected=[_fact("persona-b-city", "Lima")],
        forbidden=[_fact("persona-a-city", "Oslo")],
    )

    assert score_probe(probe_answer).passed is expected_passed


def test_summary_counts_each_type_and_excludes_na_from_pass_rate() -> None:
    scores = [
        ProbeScore(probe_id="e-pass", probe_type=ProbeType.EXTRACTION, passed=True, reason="ok"),
        ProbeScore(probe_id="e-fail", probe_type=ProbeType.EXTRACTION, passed=False, reason="bad"),
        ProbeScore(probe_id="e-na", probe_type=ProbeType.EXTRACTION, passed=None, reason="na"),
        ProbeScore(probe_id="t-pass", probe_type=ProbeType.TEMPORAL, passed=True, reason="ok"),
        ProbeScore(
            probe_id="k-fail", probe_type=ProbeType.KNOWLEDGE_UPDATE, passed=False, reason="bad"
        ),
        ProbeScore(probe_id="r-na", probe_type=ProbeType.REFUSAL, passed=None, reason="na"),
        ProbeScore(
            probe_id="i-pass",
            probe_type=ProbeType.CROSS_USER_ISOLATION,
            passed=True,
            reason="ok",
        ),
    ]

    report = summarize_probe_scores(scores)
    by_type = {item.probe_type: item.tally for item in report.by_type}

    assert by_type[ProbeType.EXTRACTION].model_dump() == {
        "total": 3,
        "passed": 1,
        "failed": 1,
        "na": 1,
        "pass_rate": 0.5,
    }
    assert by_type[ProbeType.REFUSAL].pass_rate is None
    assert len(by_type) == len(ProbeType)
    assert report.overall.model_dump() == {
        "total": 7,
        "passed": 3,
        "failed": 2,
        "na": 2,
        "pass_rate": 0.6,
    }


def test_scoring_models_are_frozen_and_reject_extra_fields() -> None:
    score = ProbeScore(
        probe_id="probe",
        probe_type=ProbeType.EXTRACTION,
        passed=True,
        reason="matched",
    )

    with pytest.raises(ValidationError):
        score.passed = False
    with pytest.raises(ValidationError):
        ProbeScore.model_validate({**score.model_dump(), "extra": "forbidden"})
