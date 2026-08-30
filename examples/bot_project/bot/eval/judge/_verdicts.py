"""Tolerant verdict parsing and rubric-weight aggregation."""

from __future__ import annotations

import json
import logging
import math
from typing import assert_never

from pydantic import ValidationError

from bot.eval.judge._models import (
    JudgeProvenance,
    JudgeResponse,
    JudgeResult,
    JudgeVerdict,
    Verdict,
    VerdictLiteral,
)
from bot.eval.judge.rubrics import RubricSet, rubric_version

logger = logging.getLogger(__name__)


def result_from_output(
    rubric_set: RubricSet,
    model: str,
    raw_output: str,
    *,
    seed_applied: bool,
) -> JudgeResult:
    parsed = _parse_response(raw_output)
    if parsed is None:
        return failure_result(
            rubric_set,
            model,
            raw_output,
            seed_applied=seed_applied,
        )
    expected = {rubric.criterion for rubric in rubric_set.rubrics}
    received: dict[str, JudgeVerdict] = {}
    for verdict in parsed.verdicts:
        if verdict.criterion not in expected:
            logger.warning("Ignoring unknown judge criterion: %s", verdict.criterion)
            continue
        received[verdict.criterion] = verdict
    verdicts = [
        received.get(
            rubric.criterion,
            JudgeVerdict(
                criterion=rubric.criterion,
                verdict=Verdict.NA.value,
                evidence="",
            ),
        )
        for rubric in rubric_set.rubrics
    ]
    weighted_score, na_count = _aggregate(rubric_set, verdicts)
    return JudgeResult(
        verdicts=verdicts,
        summary=parsed.summary,
        weighted_score=weighted_score,
        na_count=na_count,
        parse_ok=True,
        raw_output=raw_output,
        provenance=_provenance(rubric_set, model, seed_applied),
    )


def failure_result(
    rubric_set: RubricSet,
    model: str,
    raw_output: str,
    *,
    seed_applied: bool = False,
) -> JudgeResult:
    verdicts = [
        JudgeVerdict(
            criterion=rubric.criterion,
            verdict=Verdict.CANNOT_ASSESS.value,
            evidence="",
        )
        for rubric in rubric_set.rubrics
    ]
    return JudgeResult(
        verdicts=verdicts,
        summary="Judge result could not be assessed.",
        weighted_score=0.0,
        na_count=0,
        parse_ok=False,
        raw_output=raw_output,
        provenance=_provenance(rubric_set, model, seed_applied),
    )


def _parse_response(raw_output: str) -> JudgeResponse | None:
    decoder = json.JSONDecoder()
    for position, character in enumerate(raw_output):
        if character != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(raw_output[position:])
            return JudgeResponse.model_validate(payload)
        except (json.JSONDecodeError, ValidationError):
            continue
    return None


def _aggregate(rubric_set: RubricSet, verdicts: list[JudgeVerdict]) -> tuple[float, int]:
    by_criterion: dict[str, VerdictLiteral] = {
        verdict.criterion: verdict.verdict for verdict in verdicts
    }
    numerator: list[float] = []
    denominator: list[float] = []
    na_count = 0
    for rubric in rubric_set.rubrics:
        match Verdict(by_criterion[rubric.criterion]):
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
    score = math.fsum(numerator) / total_weight if total_weight > 0.0 else 0.0
    return score, na_count


def _provenance(
    rubric_set: RubricSet,
    model: str,
    seed_applied: bool,
) -> JudgeProvenance:
    return JudgeProvenance(
        judge_model=model,
        rubric_version=rubric_version(rubric_set),
        seed_applied=seed_applied,
    )
