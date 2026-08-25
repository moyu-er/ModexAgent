"""Tests for the centralized rubric library."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bot.eval.judge.rubrics import (
    Rubric,
    RubricSet,
    RubricValidationError,
    load_rubric_set,
    rubric_version,
)
from pydantic import ValidationError


def test_load_general_agent_set_has_expected_dimensions_and_weights() -> None:
    # Given: the committed general-agent rubric library.
    # When: the default library loader resolves the set.
    rubric_set = load_rubric_set("general-agent")

    # Then: all five ticket-03 dimensions and their intentional weights load.
    assert rubric_set.name == "general-agent"
    assert len(rubric_set.rubrics) == 5
    assert [rubric.criterion for rubric in rubric_set.rubrics] == [
        "task_completion",
        "empirical_verification",
        "instruction_following",
        "grounded_reporting",
        "efficiency",
    ]
    assert [rubric.weight for rubric in rubric_set.rubrics] == [
        0.35,
        0.2,
        0.2,
        0.15,
        0.1,
    ]
    assert sum(rubric.weight for rubric in rubric_set.rubrics) == pytest.approx(1.0)


def test_load_rejects_weights_that_do_not_sum_to_one(tmp_path: Path) -> None:
    # Given: a set whose named criteria carry an invalid total weight.
    (tmp_path / "bad-weights.json").write_text(
        '{"name":"bad-weights","rubrics":['
        '{"criterion":"completion","description":"Judge completion.","weight":0.6},'
        '{"criterion":"verification","description":"Judge verification.","weight":0.3}'
        "]}",
        encoding="utf-8",
    )

    # When / Then: loading rejects it and identifies every affected criterion.
    with pytest.raises(RubricValidationError) as error:
        load_rubric_set("bad-weights", library_dir=tmp_path)

    assert "weights sum to 0.9" in str(error.value)
    assert "completion" in str(error.value)
    assert "verification" in str(error.value)


def test_load_rejects_duplicate_criterion_names(tmp_path: Path) -> None:
    # Given: two independently described rubrics with the same criterion name.
    (tmp_path / "duplicates.json").write_text(
        '{"name":"duplicates","rubrics":['
        '{"criterion":"completion","description":"Judge completion.","weight":0.5},'
        '{"criterion":"completion","description":"Judge another outcome.","weight":0.5}'
        "]}",
        encoding="utf-8",
    )

    # When / Then: loading rejects it and names the overlapping criterion.
    with pytest.raises(RubricValidationError, match="completion"):
        load_rubric_set("duplicates", library_dir=tmp_path)


def test_rubric_version_is_stable_for_same_content() -> None:
    # Given: two independently constructed sets with identical typed content.
    first = RubricSet(
        name="stable",
        rubrics=[Rubric(criterion="quality", description="Judge quality.", weight=1.0)],
    )
    second = RubricSet.model_validate(first.model_dump())

    # When: each set is versioned.
    first_version = rubric_version(first)
    second_version = rubric_version(second)

    # Then: the SHA-8 values are identical and correctly shaped.
    assert first_version == second_version
    assert re.fullmatch(r"[0-9a-f]{8}", first_version)


def test_rubric_version_changes_when_content_changes() -> None:
    # Given: one judge-actionable description differs by a single character.
    first = RubricSet(
        name="changed",
        rubrics=[Rubric(criterion="quality", description="Judge quality.", weight=1.0)],
    )
    second = RubricSet(
        name="changed",
        rubrics=[Rubric(criterion="quality", description="Judge quality!", weight=1.0)],
    )

    # When / Then: the content-addressed versions differ.
    assert rubric_version(first) != rubric_version(second)


def test_rubric_version_is_independent_of_json_key_order(tmp_path: Path) -> None:
    # Given: equivalent files whose object keys use different orders.
    (tmp_path / "first.json").write_text(
        '{"name":"ordered","rubrics":['
        '{"criterion":"quality","description":"Judge quality.","weight":1.0}'
        "]}",
        encoding="utf-8",
    )
    (tmp_path / "second.json").write_text(
        '{"rubrics":['
        '{"weight":1.0,"description":"Judge quality.","criterion":"quality"}'
        '],"name":"ordered"}',
        encoding="utf-8",
    )

    # When: both files are parsed through the typed loader.
    first = load_rubric_set("first", library_dir=tmp_path)
    second = load_rubric_set("second", library_dir=tmp_path)

    # Then: canonical key sorting produces the same version.
    assert rubric_version(first) == rubric_version(second)


def test_load_missing_set_reports_searched_path(tmp_path: Path) -> None:
    # Given: an empty rubric library directory.
    expected_path = tmp_path / "missing.json"

    # When / Then: loading raises FileNotFoundError with the searched path.
    with pytest.raises(FileNotFoundError) as error:
        load_rubric_set("missing", library_dir=tmp_path)

    assert str(expected_path) in str(error.value)


def test_load_invalid_json_reports_parse_failure(tmp_path: Path) -> None:
    # Given: a malformed JSON document.
    (tmp_path / "invalid.json").write_text('{"name":', encoding="utf-8")

    # When / Then: Pydantic reports a clear JSON parsing failure.
    with pytest.raises(ValidationError, match="Invalid JSON"):
        load_rubric_set("invalid", library_dir=tmp_path)


def test_load_rejects_extra_rubric_fields(tmp_path: Path) -> None:
    # Given: a rubric entry with an undeclared field.
    (tmp_path / "extra.json").write_text(
        '{"name":"extra","rubrics":['
        '{"criterion":"quality","description":"Judge quality.",'
        '"weight":1.0,"unexpected":true}'
        "]}",
        encoding="utf-8",
    )

    # When / Then: strict Pydantic parsing rejects the extra field.
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_rubric_set("extra", library_dir=tmp_path)


def test_rubric_models_are_frozen() -> None:
    # Given: a validated rubric value object.
    rubric = Rubric(criterion="quality", description="Judge quality.", weight=1.0)

    # When / Then: field reassignment is rejected by the frozen model contract.
    with pytest.raises(ValidationError, match="Instance is frozen"):
        rubric.weight = 0.5
