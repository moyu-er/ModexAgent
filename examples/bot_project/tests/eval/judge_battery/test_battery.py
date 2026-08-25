"""Data-driven adversarial battery for memory-judge behavior."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from bot.eval.judge._models import JudgeResponse, JudgeResult, Verdict
from bot.eval.judge.memory_judge import (
    JudgeVerdictFlag,
    MemoryJudge,
    MemoryJudgeInput,
)
from pydantic import BaseModel, ConfigDict, TypeAdapter

from tests.eval.judge.test_runner import RecordingProvider

_CASES_PATH = Path(__file__).with_name("cases.json")


class VerdictDistribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    met: int = 0
    unmet: int = 0
    na: int = 0
    cannot_assess: int = 0


class BatteryCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    input: MemoryJudgeInput
    judge_output: JudgeResponse
    expected_distribution: VerdictDistribution
    expected_flags: list[JudgeVerdictFlag]


def _load_cases() -> list[BatteryCase]:
    return TypeAdapter(list[BatteryCase]).validate_json(_CASES_PATH.read_text(encoding="utf-8"))


CASES = _load_cases()


def _distribution(result: JudgeResult) -> VerdictDistribution:
    counts = Counter(Verdict(item.verdict) for item in result.verdicts)
    return VerdictDistribution(
        met=counts[Verdict.MET],
        unmet=counts[Verdict.UNMET],
        na=counts[Verdict.NA],
        cannot_assess=counts[Verdict.CANNOT_ASSESS],
    )


def _assert_expected(result: JudgeResult, case: BatteryCase) -> None:
    assert _distribution(result) == case.expected_distribution
    assert result.verdicts[0].flags == case.expected_flags


def test_battery_contains_exactly_thirty_independent_cases() -> None:
    assert len(CASES) == 30
    assert len({case.case_id for case in CASES}) == 30


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.case_id)
async def test_scripted_judge_matches_known_correct_decision(case: BatteryCase) -> None:
    # Given: one independent adversarial case and its scripted judge response.
    provider = RecordingProvider([case.judge_output.model_dump_json()])

    # When: the real memory-judge wrapper reviews the answer package.
    result = await MemoryJudge(provider).review(case.input)

    # Then: the verdict distribution and specialization flags match the frozen oracle.
    _assert_expected(result, case)


async def test_battery_oracle_rejects_a_deliberately_wrong_judgment() -> None:
    # Given: a refusal case known to be UNMET, but a scripted judge claims MET.
    case = next(item for item in CASES if item.case_id == "refusal-answers-unknown")
    wrong_output = JudgeResponse.model_validate(
        {
            "verdicts": [
                {
                    "criterion": "memory_answer",
                    "verdict": "MET",
                    "evidence": "No relevant memory is available",
                }
            ],
            "summary": "Deliberately wrong.",
        }
    )
    provider = RecordingProvider([wrong_output.model_dump_json()])

    # When: the wrong judgment runs through the same production path.
    wrong_result = await MemoryJudge(provider).review(case.input)

    # Then: the frozen oracle detects it; replacing a scripted answer makes the battery red.
    with pytest.raises(AssertionError):
        _assert_expected(wrong_result, case)
