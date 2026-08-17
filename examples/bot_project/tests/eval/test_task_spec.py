from __future__ import annotations

import pytest
from bot.eval.task_spec import (
    CommandExitAssertion,
    EvalItemSpec,
    EvalToolset,
    EvalTurn,
    FileAbsentAssertion,
    FileContainsAssertion,
    FileExistsAssertion,
)
from pydantic import ValidationError


def test_world_assertion_discriminator_routes_all_kinds() -> None:
    spec = EvalItemSpec.model_validate(
        {
            "id": "all-assertions",
            "turns": [{"user": "Inspect the workspace"}],
            "world_assertions": [
                {"kind": "file_exists", "path": "created.txt"},
                {"kind": "file_absent", "path": "removed.txt"},
                {
                    "kind": "file_contains",
                    "path": "report.txt",
                    "content": "complete",
                },
                {
                    "kind": "command_exit",
                    "command": ["python", "-m", "pytest"],
                },
            ],
        }
    )

    assert [type(assertion) for assertion in spec.world_assertions] == [
        FileExistsAssertion,
        FileAbsentAssertion,
        FileContainsAssertion,
        CommandExitAssertion,
    ]
    assert CommandExitAssertion(kind="command_exit", command=["true"]).expected_exit == 0


@pytest.mark.parametrize(
    "assertion",
    [
        {"kind": "file_exists"},
        {"kind": "file_absent"},
        {"kind": "file_contains", "path": "report.txt"},
        {"kind": "command_exit"},
    ],
)
def test_world_assertion_requires_variant_fields(assertion: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        EvalItemSpec.model_validate(
            {
                "id": "missing-required-field",
                "turns": [{"user": "Run the task"}],
                "world_assertions": [assertion],
            }
        )


def test_world_assertion_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        EvalItemSpec.model_validate(
            {
                "id": "unknown-assertion",
                "turns": [{"user": "Run the task"}],
                "world_assertions": [{"kind": "directory_exists", "path": "out"}],
            }
        )


def test_schema_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        EvalItemSpec.model_validate(
            {
                "id": "extra-field",
                "turns": [{"user": "Run the task", "unexpected": True}],
            }
        )


def test_schema_is_frozen() -> None:
    spec = EvalItemSpec(id="frozen", turns=[EvalTurn(user="Run the task")])

    with pytest.raises(ValidationError):
        spec.__setattr__("id", "changed")


@pytest.mark.parametrize("legacy_input", ["legacy query", {"query": "legacy query"}])
def test_from_item_input_detects_legacy_inputs(legacy_input: object) -> None:
    assert EvalItemSpec.from_item_input(legacy_input) is None


def test_from_item_input_validates_multi_turn_spec() -> None:
    spec = EvalItemSpec.from_item_input(
        {
            "id": "multi-turn",
            "turns": [
                {"user": "Create report.txt"},
                {"user": "Now summarize it", "expected_stop": "completed"},
            ],
            "toolset": "read_write",
        }
    )

    assert spec == EvalItemSpec(
        id="multi-turn",
        turns=[
            EvalTurn(user="Create report.txt"),
            EvalTurn(user="Now summarize it", expected_stop="completed"),
        ],
        toolset=EvalToolset.READ_WRITE,
    )


def test_turns_requires_at_least_one_turn() -> None:
    with pytest.raises(ValidationError):
        EvalItemSpec(id="empty", turns=[])


def test_optional_collections_default_empty() -> None:
    spec = EvalItemSpec(id="defaults", turns=[EvalTurn(user="Run the task")])

    assert spec.deny_tools == []
    assert spec.world_setup == {}
    assert spec.world_assertions == []
    assert spec.metadata == {}


def test_eval_toolset_has_exact_members_and_values() -> None:
    assert {member.name: member.value for member in EvalToolset} == {
        "NONE": "none",
        "READ_ONLY": "read_only",
        "READ_WRITE": "read_write",
        "FULL": "full",
    }
