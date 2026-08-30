from __future__ import annotations

import pytest
from bot.eval.judge.calibration import (
    CalibrationInputError,
    ConfusionMatrix,
    VerdictWithMeta,
    bias_audit_long_short,
    cohen_kappa,
    direction_skew,
    na_rate,
)
from bot.eval.judge.calibration import (
    test_retest as measure_test_retest,
)
from bot.eval.judge.runner import Verdict
from pydantic import ValidationError


def test_cohen_kappa_is_one_for_perfect_binary_agreement() -> None:
    # Given
    verdicts = [Verdict.MET, Verdict.UNMET, Verdict.MET, Verdict.UNMET]

    # When
    result = cohen_kappa(verdicts, verdicts)

    # Then
    assert result.value == 1.0
    assert result.matrix == ConfusionMatrix(
        met_met=2,
        met_unmet=0,
        unmet_met=0,
        unmet_unmet=2,
    )


def test_cohen_kappa_matches_hand_computed_mixed_example() -> None:
    # Given: Po=7/10 and Pe=1/2, so kappa=(0.7-0.5)/(1-0.5)=0.4.
    judge = [Verdict.MET] * 5 + [Verdict.UNMET] * 5
    human = [
        Verdict.MET,
        Verdict.MET,
        Verdict.MET,
        Verdict.UNMET,
        Verdict.UNMET,
        Verdict.MET,
        Verdict.UNMET,
        Verdict.UNMET,
        Verdict.UNMET,
        Verdict.UNMET,
    ]

    # When
    result = cohen_kappa(judge, human)

    # Then
    assert result.value == 0.4
    assert result.matrix == ConfusionMatrix(
        met_met=3,
        met_unmet=2,
        unmet_met=1,
        unmet_unmet=4,
    )


@pytest.mark.parametrize(
    ("judge", "human"),
    [
        ([], []),
        ([Verdict.NA, Verdict.CANNOT_ASSESS], [Verdict.MET, Verdict.UNMET]),
    ],
)
def test_cohen_kappa_reports_undefined_without_binary_pairs(
    judge: list[Verdict],
    human: list[Verdict],
) -> None:
    # Given
    result = cohen_kappa(judge, human)

    # Then
    assert result.value is None
    assert result.matrix == ConfusionMatrix()


def test_cohen_kappa_rejects_mismatched_lengths() -> None:
    # Given / When / Then
    with pytest.raises(CalibrationInputError, match="same length"):
        cohen_kappa([Verdict.MET], [])


def test_na_rate_counts_na_and_cannot_assess() -> None:
    # Given
    verdicts = [Verdict.MET, Verdict.UNMET, Verdict.NA, Verdict.CANNOT_ASSESS]

    # When / Then
    assert na_rate(verdicts) == 0.5


@pytest.mark.parametrize(
    ("matrix", "ratio", "trigger"),
    [
        (ConfusionMatrix(met_unmet=2, unmet_met=1), 2.0, False),
        (ConfusionMatrix(met_unmet=201, unmet_met=100), 2.01, True),
        (ConfusionMatrix(met_unmet=1, unmet_met=0), float("inf"), True),
    ],
)
def test_direction_skew_uses_strictly_greater_than_two_boundary(
    matrix: ConfusionMatrix,
    ratio: float,
    trigger: bool,
) -> None:
    # When
    result = direction_skew(matrix)

    # Then
    assert result.ratio == ratio
    assert result.trigger is trigger


def test_bias_audit_splits_answer_lengths_and_reports_known_gap() -> None:
    # Given: short answers are all MET; long answers are half MET.
    items = [
        VerdictWithMeta(verdict=Verdict.MET, answer_length=length)
        for length in range(1, 5)
    ] + [
        VerdictWithMeta(
            verdict=Verdict.MET if length < 7 else Verdict.UNMET,
            answer_length=length,
        )
        for length in range(5, 9)
    ]

    # When
    result = bias_audit_long_short(items)

    # Then
    assert result.short_met_rate == 1.0
    assert result.long_met_rate == 0.5
    assert result.long_short_gap_pp == 50.0
    assert result.trigger is True


def test_retest_requires_three_repeats_and_counts_slot_unanimity() -> None:
    # Given
    reviews = [
        [Verdict.MET, Verdict.UNMET, Verdict.MET, Verdict.UNMET],
        [Verdict.MET, Verdict.UNMET, Verdict.MET, Verdict.UNMET],
        [Verdict.MET, Verdict.UNMET, Verdict.UNMET, Verdict.UNMET],
    ]

    # When
    result = measure_test_retest(reviews)

    # Then
    assert result.agreement == 0.75
    assert result.passes is False


@pytest.mark.parametrize(("agreeing", "passes"), [(950, True), (949, False)])
def test_retest_threshold_boundary(agreeing: int, passes: bool) -> None:
    # Given
    baseline = [Verdict.MET] * 1000
    changed = [Verdict.MET] * agreeing + [Verdict.UNMET] * (1000 - agreeing)

    # When
    result = measure_test_retest([baseline, baseline, changed])

    # Then
    assert result.agreement == agreeing / 1000
    assert result.passes is passes


def test_retest_rejects_fewer_than_three_or_mismatched_repeats() -> None:
    # Given / When / Then
    with pytest.raises(CalibrationInputError, match="at least 3"):
        measure_test_retest([[Verdict.MET], [Verdict.MET]])
    with pytest.raises(CalibrationInputError, match="same length"):
        measure_test_retest([[Verdict.MET], [Verdict.MET], []])


def test_calibration_results_are_frozen_and_forbid_extra_fields() -> None:
    # Given
    matrix = ConfusionMatrix()

    # When / Then
    with pytest.raises(ValidationError):
        matrix.met_met = 1
    with pytest.raises(ValidationError):
        ConfusionMatrix.model_validate({"unknown": 1})
