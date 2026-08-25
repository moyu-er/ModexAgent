"""Workspace and memory assertion evaluation for the B7 sentinel."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import assert_never

from evals.sentinel.tasks import SentinelTask

from bot.eval.sentinel.results import AssertionResult, SentinelTaskObservation, SentinelTaskStatus
from bot.eval.task_spec import (
    CommandExitAssertion,
    FileAbsentAssertion,
    FileContainsAssertion,
    FileExistsAssertion,
)


def evaluate_observation(
    task: SentinelTask,
    workspace: Path,
    output: str,
    error: str | None = None,
) -> SentinelTaskObservation:
    """Evaluate task output against real workspace and memory-dependent evidence."""
    world: list[AssertionResult] = []
    for assertion in task.world_assertions:
        match assertion:
            case FileExistsAssertion(path=raw_path):
                passed = (workspace / raw_path).is_file()
            case FileAbsentAssertion(path=raw_path):
                passed = not (workspace / raw_path).exists()
            case FileContainsAssertion(path=raw_path, content=content):
                path = workspace / raw_path
                passed = path.is_file() and content in path.read_text(encoding="utf-8")
            case CommandExitAssertion(command=command, expected_exit=expected):
                completed = subprocess.run(command, cwd=workspace, check=False, timeout=60)
                passed = completed.returncode == expected
            case unreachable:
                assert_never(unreachable)
        world.append(
            AssertionResult(assertion_id=f"world:{assertion.kind}:{len(world)}", passed=passed)
        )

    artifact_text = (
        output
        + "\n"
        + "\n".join(
            path.relative_to(workspace).as_posix() + "\n" + path.read_text(encoding="utf-8")
            for path in workspace.rglob("*")
            if path.is_file()
        )
    )
    memory = tuple(
        AssertionResult(
            assertion_id=item.fact_id,
            passed=item.must_contain in artifact_text,
            details=None if item.must_contain in artifact_text else f"missing {item.must_contain}",
        )
        for item in task.memory_assertions
    )
    passed = (
        error is None and all(item.passed for item in world) and all(item.passed for item in memory)
    )
    return SentinelTaskObservation(
        status=SentinelTaskStatus.SUCCESS if passed else SentinelTaskStatus.FAILED,
        world_assertions=tuple(world),
        memory_assertions=memory,
        error=error,
    )


__all__ = ["evaluate_observation"]
