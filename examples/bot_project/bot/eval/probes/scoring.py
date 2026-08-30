from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final, assert_never

from pydantic import BaseModel, ConfigDict, Field

from bot.eval.probes.schema import Fact, ProbeType


# fmt: off
def _normalize_evidence(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.casefold()).split())


def recalls_all_evidence(text: str, expected_evidence: Sequence[str]) -> bool:
    normalized_text = _normalize_evidence(text)
    return all(
        _normalize_evidence(evidence) in normalized_text
        for evidence in expected_evidence
    )


def has_no_isolation_contamination(
    text: str,
    forbidden_evidence: Sequence[str],
) -> bool:
    normalized_text = _normalize_evidence(text)
    return all(
        _normalize_evidence(evidence) not in normalized_text
        for evidence in forbidden_evidence
    )
# fmt: on


class ProbeAnswer(BaseModel):
    """Answer package derived from one validated probe and its fact references."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(min_length=1)
    probe_type: ProbeType
    answer: str | None
    injected_context: str | None
    expected_evidence: list[Fact] = Field(default_factory=list)
    forbidden_evidence: list[Fact] = Field(default_factory=list)


class ProbeScore(BaseModel):
    """One deterministic probe verdict; None means insufficient data (NA)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_id: str = Field(min_length=1)
    probe_type: ProbeType
    passed: bool | None
    reason: str = Field(min_length=1)


class ScoreTally(BaseModel):
    """Counts and conclusive-only pass rate for a score collection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    na: int = Field(ge=0)
    pass_rate: float | None = Field(ge=0.0, le=1.0)


class ProbeTypeReport(BaseModel):
    """Aggregate counts for one of the five orthogonal probe types."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_type: ProbeType
    tally: ScoreTally


class ProbeScoringReport(BaseModel):
    """Five-type deterministic score report and overall pass rate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    by_type: list[ProbeTypeReport]
    overall: ScoreTally


_REFUSAL_PHRASES: Final = (
    "不知道",
    "不清楚",
    "未提供",
    "没有提供",
    "未曾提及",
    "无法回答",
    "do not know",
    "don t know",
    "not provided",
    "no stored information",
    "cannot answer",
    "can t answer",
    "refusal",
    "refuse",
)


def memory_retrieval_recall(probe_answer: ProbeAnswer) -> ProbeScore:
    """Score expected fact surfaces against the final injected context."""
    context = probe_answer.injected_context
    if context is None:
        return _score(probe_answer, None, "final injected context is unavailable")
    evidence = [surface for fact in probe_answer.expected_evidence for surface in fact.surface_refs]
    passed = recalls_all_evidence(context, evidence)
    reason = "all expected evidence was recalled" if passed else "expected evidence was missing"
    return _score(probe_answer, passed, reason)


def cross_user_isolation(probe_answer: ProbeAnswer) -> ProbeScore:
    """Score signal 1 context contamination only.

    Answer-layer leakage is signal 2 and belongs to the ticket-24 judge; this
    deterministic scorer deliberately does not inspect the answer.
    """
    context = probe_answer.injected_context
    if context is None:
        return _score(probe_answer, None, "final injected context is unavailable")
    forbidden = [
        surface for fact in probe_answer.forbidden_evidence for surface in fact.surface_refs
    ]
    passed = has_no_isolation_contamination(context, forbidden)
    reason = "context contains no cross-user evidence" if passed else "context is contaminated"
    return _score(probe_answer, passed, reason)


def score_probe(probe_answer: ProbeAnswer) -> ProbeScore:
    """Dispatch one answer package to its exhaustive deterministic scorer."""
    match probe_answer.probe_type:
        case ProbeType.EXTRACTION:
            return memory_retrieval_recall(probe_answer)
        case ProbeType.TEMPORAL:
            return _score_temporal(probe_answer)
        case ProbeType.KNOWLEDGE_UPDATE:
            return _score_knowledge_update(probe_answer)
        case ProbeType.REFUSAL:
            return _score_refusal(probe_answer)
        case ProbeType.CROSS_USER_ISOLATION:
            return cross_user_isolation(probe_answer)
        case unreachable:
            assert_never(unreachable)


def summarize_probe_scores(scores: Sequence[ProbeScore]) -> ProbeScoringReport:
    """Count all five types and compute pass rates without treating NA as zero."""
    by_type = [
        ProbeTypeReport(
            probe_type=probe_type,
            tally=_tally([score for score in scores if score.probe_type == probe_type]),
        )
        for probe_type in ProbeType
    ]
    return ProbeScoringReport(by_type=by_type, overall=_tally(scores))


def _score_temporal(probe_answer: ProbeAnswer) -> ProbeScore:
    answer = probe_answer.answer
    if answer is None or not answer.strip():
        return _score(probe_answer, None, "answer is unavailable")
    ordered = sorted(probe_answer.expected_evidence, key=lambda fact: fact.valid_from)
    normalized_answer = _normalize_evidence(answer)
    positions = [normalized_answer.find(_normalize_evidence(fact.value)) for fact in ordered]
    present = all(position >= 0 for position in positions)
    chronological = all(left < right for left, right in zip(positions, positions[1:], strict=False))
    passed = present and chronological
    reason = (
        "fact values follow valid_from order"
        if passed
        else "fact values are missing or out of order"
    )
    return _score(probe_answer, passed, reason)


def _score_knowledge_update(probe_answer: ProbeAnswer) -> ProbeScore:
    answer = probe_answer.answer
    if answer is None or not answer.strip():
        return _score(probe_answer, None, "answer is unavailable")
    expected = [fact.value for fact in probe_answer.expected_evidence]
    superseded = [fact.value for fact in probe_answer.forbidden_evidence]
    has_current = recalls_all_evidence(answer, expected)
    omits_superseded = has_no_isolation_contamination(answer, superseded)
    passed = has_current and omits_superseded
    reason = (
        "answer uses only current truth"
        if passed
        else "current truth is missing or superseded truth is used"
    )
    return _score(probe_answer, passed, reason)


def _score_refusal(probe_answer: ProbeAnswer) -> ProbeScore:
    answer = probe_answer.answer
    if answer is None or not answer.strip():
        return _score(probe_answer, None, "answer is unavailable")
    normalized_answer = _normalize_evidence(answer)
    passed = any(phrase in normalized_answer for phrase in _REFUSAL_PHRASES)
    reason = (
        "answer honestly refuses unsupported truth"
        if passed
        else "answer does not refuse unsupported truth"
    )
    return _score(probe_answer, passed, reason)


def _score(probe_answer: ProbeAnswer, passed: bool | None, reason: str) -> ProbeScore:
    return ProbeScore(
        probe_id=probe_answer.probe_id,
        probe_type=probe_answer.probe_type,
        passed=passed,
        reason=reason,
    )


def _tally(scores: Sequence[ProbeScore]) -> ScoreTally:
    passed = sum(score.passed is True for score in scores)
    failed = sum(score.passed is False for score in scores)
    na = sum(score.passed is None for score in scores)
    conclusive = passed + failed
    pass_rate = passed / conclusive if conclusive else None
    return ScoreTally(
        total=len(scores),
        passed=passed,
        failed=failed,
        na=na,
        pass_rate=pass_rate,
    )
