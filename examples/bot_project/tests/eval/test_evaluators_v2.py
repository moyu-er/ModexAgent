from __future__ import annotations

import pytest
from bot.eval.evaluators import tool_success_evaluator, world_state_evaluator


def test_world_state_all_passed_is_true() -> None:
    evaluation = world_state_evaluator(
        input={"id": "create-report", "turns": [{"user": "Create report.txt"}]},
        output={
            "world_results": [
                {
                    "assertion": "file_exists:created.txt",
                    "passed": True,
                    "detail": "path exists",
                },
                {
                    "assertion": "file_contains:report.txt",
                    "passed": True,
                    "detail": "content found",
                },
            ]
        },
    )

    assert evaluation.name == "world_state"
    assert evaluation.value is True
    assert evaluation.data_type == "BOOLEAN"
    assert "All 2 world assertions passed" in evaluation.comment


def test_world_state_one_failed_is_false_with_label_in_comment() -> None:
    evaluation = world_state_evaluator(
        input={"id": "mixed", "turns": [{"user": "Run the task"}]},
        output={
            "world_results": [
                {
                    "assertion": "file_exists:created.txt",
                    "passed": True,
                    "detail": "path exists",
                },
                {
                    "assertion": "file_absent:removed.txt",
                    "passed": False,
                    "detail": "path still exists",
                },
                {
                    "assertion": "command_exit:pytest",
                    "passed": False,
                    "detail": "exit code 1",
                },
            ]
        },
    )

    assert evaluation.value is False
    assert evaluation.data_type == "BOOLEAN"
    assert "file_absent:removed.txt" in evaluation.comment
    assert "command_exit:pytest" in evaluation.comment


def test_world_state_missing_world_results_is_false() -> None:
    evaluation = world_state_evaluator(
        input={"id": "no-dump", "turns": [{"user": "Run the task"}]},
        output={"stop_reason": "completed"},
    )

    assert evaluation.value is False
    assert evaluation.data_type == "BOOLEAN"
    assert "no world results" in evaluation.comment.lower()


def test_world_state_non_dict_output_is_false() -> None:
    evaluation = world_state_evaluator(
        input={"id": "prose", "turns": [{"user": "Run the task"}]},
        output="I created the file successfully.",
    )

    assert evaluation.value is False
    assert evaluation.data_type == "BOOLEAN"


def test_tool_success_reads_success_rate_as_numeric() -> None:
    evaluation = tool_success_evaluator(
        input={"id": "tools", "turns": [{"user": "Run the task"}]},
        output={
            "tool_stats": {
                "total": 6,
                "errors": 1,
                "success_rate": 0.8335,
                "source": "spans",
            }
        },
    )

    assert evaluation.name == "tool_success"
    assert evaluation.value == 0.8335
    assert evaluation.data_type == "NUMERIC"


def test_tool_success_zero_total_scores_one() -> None:
    evaluation = tool_success_evaluator(
        input={"id": "no-tools", "turns": [{"user": "Just answer"}]},
        output={"tool_stats": {"total": 0, "errors": 0, "success_rate": 0.0}},
    )

    assert evaluation.value == 1.0
    assert evaluation.data_type == "NUMERIC"
    assert "no tool calls" in evaluation.comment.lower()


@pytest.mark.parametrize("output", [None, "plain prose", {"stop_reason": "completed"}])
def test_tool_success_missing_stats_scores_zero(output: object) -> None:
    evaluation = tool_success_evaluator(
        input={"id": "no-stats", "turns": [{"user": "Run the task"}]},
        output=output,
    )

    assert evaluation.value == 0.0
    assert evaluation.data_type == "NUMERIC"
    assert "no tool stats" in evaluation.comment.lower()
