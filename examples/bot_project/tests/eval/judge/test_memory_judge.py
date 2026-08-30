"""Pure policy and runner-composition tests for the memory judge."""

from __future__ import annotations

import pytest
from bot.eval.judge._models import (
    JudgeProvenance,
    JudgeResult,
    JudgeVerdict,
    VerdictLiteral,
)
from bot.eval.judge.memory_judge import (
    JudgeVerdictFlag,
    KnowledgeUpdateTier,
    MemoryJudge,
    MemoryJudgeInput,
    MemoryJudgeSettings,
    MemoryTruth,
    SameModelJudgeError,
    apply_citation_gate,
    classify_knowledge_update,
)
from bot.eval.judge.rubrics import Rubric, RubricSet
from bot.eval.probes.schema import ProbeType

from .test_runner import RecordingProvider


def _rubric_set() -> RubricSet:
    return RubricSet(
        name="memory-test",
        rubrics=[
            Rubric(
                criterion="memory_answer",
                description="Judge the memory probe answer.",
                weight=1.0,
            )
        ],
    )


def _result(*, verdict: VerdictLiteral, evidence: str) -> JudgeResult:
    return JudgeResult(
        verdicts=[
            JudgeVerdict(
                criterion="memory_answer",
                verdict=verdict,
                evidence=evidence,
            )
        ],
        summary="Scripted.",
        weighted_score=1.0 if verdict == "MET" else 0.0,
        na_count=0,
        parse_ok=True,
        raw_output="{}",
        provenance=JudgeProvenance(
            judge_model="judge/test-model",
            rubric_version="12345678",
            seed_applied=True,
        ),
    )


def _input(*, answer_model: str) -> MemoryJudgeInput:
    return MemoryJudgeInput(
        probe_type=ProbeType.EXTRACTION,
        question="What project code was stored?",
        truth=MemoryTruth(expected_answers=["ALPHA-42"]),
        candidate_answer="The project code is ALPHA-42.",
        injected_context="Memory says: Project code — ALPHA-42.",
        answer_model=answer_model,
    )


def test_citation_gate_accepts_normalized_context_quote() -> None:
    # Given: a MET verdict whose quote differs only by punctuation and case.
    result = _result(verdict="MET", evidence="memory says project code alpha 42")

    # When: the deterministic citation gate checks the assembled context.
    gated = apply_citation_gate(result, "Memory says: Project code — ALPHA-42.", _rubric_set())

    # Then: the valid citation and score pass through unchanged.
    assert gated.verdicts[0].verdict == "MET"
    assert gated.verdicts[0].flags == []
    assert gated.weighted_score == 1.0


def test_citation_gate_downgrades_fabricated_quote() -> None:
    # Given: a MET verdict citing text absent from the assembled context.
    result = _result(verdict="MET", evidence="The project code is OMEGA-99.")

    # When: the deterministic citation gate checks the quote.
    gated = apply_citation_gate(result, "Memory says: Project code — ALPHA-42.", _rubric_set())

    # Then: only that verdict is downgraded and marked as fabricated.
    assert gated.verdicts[0].verdict == "UNMET"
    assert gated.verdicts[0].flags == [JudgeVerdictFlag.CITATION_FABRICATED]
    assert gated.weighted_score == 0.0


@pytest.mark.parametrize(
    ("candidate", "expected_tier", "expected_verdict", "expected_flags"),
    [
        ("The new office is Harbor Point.", KnowledgeUpdateTier.CURRENT, "MET", []),
        (
            "The office remains Cedar Lane.",
            KnowledgeUpdateTier.STALE,
            "UNMET",
            [JudgeVerdictFlag.STALE],
        ),
        ("I cannot determine the office.", KnowledgeUpdateTier.NEITHER, "UNMET", []),
    ],
)
def test_knowledge_update_three_tiers_are_verbatim(
    candidate: str,
    expected_tier: KnowledgeUpdateTier,
    expected_verdict: VerdictLiteral,
    expected_flags: list[JudgeVerdictFlag],
) -> None:
    # Given / When: a candidate is classified against current and stale values.
    decision = classify_knowledge_update(candidate, ["Harbor Point"], ["Cedar Lane"])

    # Then: current/stale/neither maps exactly to MET/UNMET+stale/UNMET.
    assert decision.tier is expected_tier
    assert decision.verdict == expected_verdict
    assert decision.flags == expected_flags


async def test_same_answer_and_judge_model_refuses_before_provider_call() -> None:
    # Given: the answer and judge identify the exact same model.
    provider = RecordingProvider([])
    judge = MemoryJudge(provider)

    # When / Then: review refuses before any provider call.
    with pytest.raises(SameModelJudgeError, match="judge/test-model"):
        await judge.review(_input(answer_model="judge/test-model"))
    assert provider.messages == []


async def test_allow_same_model_override_runs_and_is_audited() -> None:
    # Given: a same-model run with the explicit auditable override enabled.
    provider = RecordingProvider(
        [
            '{"verdicts":[{"criterion":"memory_answer","verdict":"MET",'
            '"evidence":"Project code ALPHA-42"}],"summary":"Correct."}'
        ]
    )
    judge = MemoryJudge(provider, MemoryJudgeSettings(allow_same_model=True))

    # When: the memory review runs.
    result = await judge.review(_input(answer_model="judge/test-model"))

    # Then: the override and both model identities are retained in provenance.
    assert result.verdicts[0].verdict == "MET"
    assert result.provenance.answer_model == "judge/test-model"
    assert result.provenance.same_model_override is True
    assert len(provider.messages) == 1


async def test_distinct_models_run_without_noncompliant_override() -> None:
    # Given: independent answer and judge models.
    provider = RecordingProvider(
        [
            '{"verdicts":[{"criterion":"memory_answer","verdict":"MET",'
            '"evidence":"Project code ALPHA-42"}],"summary":"Correct."}'
        ]
    )

    # When: the memory review runs with default separation policy.
    result = await MemoryJudge(provider).review(_input(answer_model="answer/test-model"))

    # Then: the run is compliant and does not claim an override.
    assert result.provenance.answer_model == "answer/test-model"
    assert result.provenance.same_model_override is False
