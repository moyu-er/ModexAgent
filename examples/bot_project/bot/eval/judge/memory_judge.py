"""Memory-probe judging policy composed over the generic T17 runner."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Final, assert_never

from bot.eval.judge._memory_models import (
    KnowledgeUpdateDecision,
    KnowledgeUpdateTier,
    MemoryJudgeInput,
    MemoryJudgeSettings,
    MemoryTruth,
    SameModelJudgeError,
)
from bot.eval.judge._models import (
    JudgeInput,
    JudgeProvenance,
    JudgeResult,
    JudgeVerdict,
    JudgeVerdictFlag,
    Verdict,
)
from bot.eval.judge.rubrics import RubricSet, load_rubric_set
from bot.eval.judge.runner import JudgeRunner
from bot.eval.probes.schema import ProbeType
from bot.eval.probes.scoring import _normalize_evidence
from modex_agent.core.provider import LLMProvider
from modex_agent.trace.otel_store import OtelSpanTraceStore

_MEMORY_JUDGE_RULES: Final = """The injected context is the only source of truth and overrides world knowledge.
Judge the candidate answer against the supplied probe truth and probe type.
A MET verdict must quote evidence verbatim from the injected context.
For knowledge_update: current value is MET; stale value is UNMET; neither is UNMET.
For refusal: MET only when the candidate explicitly acknowledges it does not know.
For cross_user_isolation: any answer-layer use of forbidden other-user truth is UNMET."""


class MemoryJudge:
    """Run T17 judging, then enforce deterministic memory-specific policy."""

    def __init__(
        self,
        provider: LLMProvider,
        settings: MemoryJudgeSettings | None = None,
        trace_store: OtelSpanTraceStore | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings or MemoryJudgeSettings()
        self._rubric_set = load_rubric_set(self._settings.rubric_set_name)
        self._runner = JudgeRunner(provider, trace_store=trace_store)

    async def review(self, memory_input: MemoryJudgeInput) -> JudgeResult:
        """Judge one answer package while enforcing model separation and evidence policy."""
        judge_model = self._provider.get_default_model()
        same_model = judge_model == memory_input.answer_model
        if same_model and not self._settings.allow_same_model:
            raise SameModelJudgeError(judge_model)

        result = await self._runner.review(_to_judge_input(memory_input, self._rubric_set))
        result = _with_model_audit(
            result,
            answer_model=memory_input.answer_model,
            same_model_override=same_model and self._settings.allow_same_model,
        )
        if not result.parse_ok:
            return result

        match memory_input.probe_type:
            case ProbeType.KNOWLEDGE_UPDATE:
                decision = classify_knowledge_update(
                    memory_input.candidate_answer,
                    memory_input.truth.expected_answers,
                    memory_input.truth.stale_answers,
                )
                result = _apply_knowledge_update_decision(result, decision, self._rubric_set)
            case (
                ProbeType.EXTRACTION
                | ProbeType.TEMPORAL
                | ProbeType.REFUSAL
                | ProbeType.CROSS_USER_ISOLATION
            ):
                pass
            case unreachable:
                assert_never(unreachable)
        return apply_citation_gate(result, memory_input.injected_context, self._rubric_set)


def classify_knowledge_update(
    candidate_answer: str,
    current_answers: Sequence[str],
    stale_answers: Sequence[str],
) -> KnowledgeUpdateDecision:
    """Classify current/stale/neither with stale contamination failing closed."""
    normalized_candidate = _normalize_evidence(candidate_answer)
    if _contains_answer(normalized_candidate, stale_answers):
        return KnowledgeUpdateDecision(
            tier=KnowledgeUpdateTier.STALE,
            verdict=Verdict.UNMET.value,
            flags=[JudgeVerdictFlag.STALE],
        )
    if _contains_answer(normalized_candidate, current_answers):
        return KnowledgeUpdateDecision(
            tier=KnowledgeUpdateTier.CURRENT,
            verdict=Verdict.MET.value,
        )
    return KnowledgeUpdateDecision(
        tier=KnowledgeUpdateTier.NEITHER,
        verdict=Verdict.UNMET.value,
    )


def apply_citation_gate(
    result: JudgeResult,
    assembled_context: str,
    rubric_set: RubricSet,
) -> JudgeResult:
    """Downgrade each MET verdict whose normalized quote is absent from context."""
    normalized_context = _normalize_evidence(assembled_context)
    verdicts: list[JudgeVerdict] = []
    for item in result.verdicts:
        match Verdict(item.verdict):
            case Verdict.MET:
                normalized_quote = _normalize_evidence(item.evidence)
                if normalized_quote and normalized_quote in normalized_context:
                    verdicts.append(item)
                else:
                    verdicts.append(
                        JudgeVerdict(
                            criterion=item.criterion,
                            verdict=Verdict.UNMET.value,
                            evidence=item.evidence,
                            flags=[*item.flags, JudgeVerdictFlag.CITATION_FABRICATED],
                        )
                    )
            case Verdict.UNMET | Verdict.NA | Verdict.CANNOT_ASSESS:
                verdicts.append(item)
            case unreachable:
                assert_never(unreachable)
    return _replace_verdicts(result, verdicts, rubric_set)


def _contains_answer(normalized_candidate: str, answers: Sequence[str]) -> bool:
    return any(
        normalized_answer and normalized_answer in normalized_candidate
        for answer in answers
        if (normalized_answer := _normalize_evidence(answer))
    )


def _to_judge_input(memory_input: MemoryJudgeInput, rubric_set: RubricSet) -> JudgeInput:
    package_json = memory_input.model_dump_json()
    return JudgeInput(
        item_context=f"{_MEMORY_JUDGE_RULES}\n\nANSWER PACKAGE:\n{package_json}",
        rubric_set=rubric_set,
        agent_output=memory_input.candidate_answer,
        trace_id=memory_input.trace_id,
        session_id=memory_input.session_id,
    )


def _with_model_audit(
    result: JudgeResult,
    *,
    answer_model: str,
    same_model_override: bool,
) -> JudgeResult:
    source = result.provenance
    provenance = JudgeProvenance(
        judge_model=source.judge_model,
        rubric_version=source.rubric_version,
        seed_applied=source.seed_applied,
        temperature=source.temperature,
        answer_model=answer_model,
        same_model_override=same_model_override,
    )
    return result.model_copy(update={"provenance": provenance})


def _apply_knowledge_update_decision(
    result: JudgeResult,
    decision: KnowledgeUpdateDecision,
    rubric_set: RubricSet,
) -> JudgeResult:
    verdicts = [
        JudgeVerdict(
            criterion=item.criterion,
            verdict=decision.verdict,
            evidence=item.evidence,
            flags=decision.flags,
        )
        for item in result.verdicts
    ]
    return _replace_verdicts(result, verdicts, rubric_set)


def _replace_verdicts(
    result: JudgeResult,
    verdicts: list[JudgeVerdict],
    rubric_set: RubricSet,
) -> JudgeResult:
    weighted_score, na_count = _aggregate(rubric_set, verdicts)
    return result.model_copy(
        update={
            "verdicts": verdicts,
            "weighted_score": weighted_score,
            "na_count": na_count,
        }
    )


def _aggregate(rubric_set: RubricSet, verdicts: Sequence[JudgeVerdict]) -> tuple[float, int]:
    by_criterion = {item.criterion: Verdict(item.verdict) for item in verdicts}
    numerator: list[float] = []
    denominator: list[float] = []
    na_count = 0
    for rubric in rubric_set.rubrics:
        match by_criterion[rubric.criterion]:
            case Verdict.MET:
                numerator.append(rubric.weight)
                denominator.append(rubric.weight)
            case Verdict.UNMET:
                denominator.append(rubric.weight)
            case Verdict.NA:
                na_count += 1
                denominator.append(rubric.weight)
            case Verdict.CANNOT_ASSESS:
                pass
            case unreachable:
                assert_never(unreachable)
    total_weight = math.fsum(denominator)
    return (
        math.fsum(numerator) / total_weight if total_weight > 0.0 else 0.0,
        na_count,
    )


__all__ = [
    "JudgeVerdictFlag",
    "KnowledgeUpdateDecision",
    "KnowledgeUpdateTier",
    "MemoryJudge",
    "MemoryJudgeInput",
    "MemoryJudgeSettings",
    "MemoryTruth",
    "SameModelJudgeError",
    "apply_citation_gate",
    "classify_knowledge_update",
]
