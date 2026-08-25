from __future__ import annotations

from datetime import UTC, datetime

import pytest
from bot.eval.live_gates.b3_linkage_runtime import ExperimentQuery, LinkageLookup
from bot.eval.sentinel import gate_cli
from bot.eval.sentinel.execution import SentinelTraceRecord
from bot.eval.sentinel.gate_cli import build_gate_evidence
from bot.eval.sentinel.orchestrator import SentinelArmRunResult, SentinelRunResult
from bot.eval.sentinel.report import generate_difference_report
from bot.eval.sentinel.results import AssertionResult, SentinelTaskResult, SentinelTaskStatus
from evals.sentinel.tasks import MEMORY_CHAIN_V1_CHAIN, SentinelArm

from modex_agent.trace.experiment_attrs import ExperimentLinkage
from modex_agent.trace.langfuse_query import ScoreReadData


class _FakeScoreClient:
    def __init__(self, comments: dict[str, str]) -> None:
        self._comments = comments

    async def get_scores(
        self,
        *,
        fields: str,
        trace_id: str,
        name: str,
    ) -> tuple[list[ScoreReadData], None]:
        _ = fields
        return (
            [
                ScoreReadData(
                    name=name,
                    value=True,
                    data_type="BOOLEAN",
                    comment=self._comments[trace_id],
                )
            ],
            None,
        )

    async def close(self) -> None:
        return


def _run_result(memory_successes: int, nomemory_successes: int) -> SentinelRunResult:
    arm_results: list[SentinelArmRunResult] = []
    all_results: list[SentinelTaskResult] = []
    for arm, successes in (
        (SentinelArm.MEMORY, memory_successes),
        (SentinelArm.NOMEMORY, nomemory_successes),
    ):
        task_results = tuple(
            SentinelTaskResult(
                task_id=task.task_id,
                arm=arm,
                status=(
                    SentinelTaskStatus.SUCCESS if index < successes else SentinelTaskStatus.FAILED
                ),
                world_assertions=(AssertionResult(assertion_id="world", passed=index < successes),),
                memory_assertions=(),
            )
            for index, task in enumerate(MEMORY_CHAIN_V1_CHAIN.tasks)
        )
        all_results.extend(task_results)
        arm_results.append(
            SentinelArmRunResult(
                arm=arm,
                experiment_name=f"memory-chain-v1.test.{arm.value}",
                task_results=task_results,
            )
        )
    return SentinelRunResult(
        run_id="test",
        seed=17,
        arms=(arm_results[0], arm_results[1]),
        report=generate_difference_report(all_results),
    )


def test_gate_passes_only_when_memory_success_count_is_strictly_higher() -> None:
    evidence = build_gate_evidence(_run_result(3, 1), actual_cost_usd=4.0, max_cost_usd=5.0)

    assert evidence.passed is True
    assert evidence.report.difference.success_count_delta == 2


def test_gate_fails_when_arms_tie_even_with_complete_data() -> None:
    evidence = build_gate_evidence(_run_result(2, 2), actual_cost_usd=4.0, max_cost_usd=5.0)

    assert evidence.passed is False
    assert evidence.error == "memory arm did not strictly outperform nomemory arm"


def test_gate_fails_when_cost_cap_is_exceeded() -> None:
    evidence = build_gate_evidence(_run_result(3, 1), actual_cost_usd=5.1, max_cost_usd=5.0)

    assert evidence.passed is False
    assert evidence.error == "cost cap exceeded"


def test_live_gate_requires_two_visible_experiments_and_all_verdict_scores() -> None:
    evidence = build_gate_evidence(
        _run_result(3, 2),
        actual_cost_usd=0.2,
        max_cost_usd=1.0,
        visible_experiments=("memory-chain-v1.test.memory",),
        verdict_score_count=5,
        require_visibility=True,
    )

    assert evidence.passed is False
    assert evidence.error == "two-arm trace/experiment visibility incomplete"


async def test_live_gate_counts_only_contract_compliant_verdict_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    run_ref = "evals/runs/sentinel/test"
    records = (
        SentinelTraceRecord(
            task_id="establish-conventions",
            arm=SentinelArm.MEMORY,
            trace_id="memory-trace",
            observation_id="memory-observation",
        ),
        SentinelTraceRecord(
            task_id="establish-conventions",
            arm=SentinelArm.NOMEMORY,
            trace_id="nomemory-trace",
            observation_id="nomemory-observation",
        ),
    )
    links = {
        (record.arm.value, record.task_id): ExperimentLinkage(
            experiment_id=f"{record.arm.value}-experiment",
            experiment_name=f"memory-chain-v1.test.{record.arm.value}",
            dataset_id="dataset-id",
            item_id=f"{record.arm.value}-item",
        )
        for record in records
    }
    score_client = _FakeScoreClient(
        {
            "memory-trace": (
                '{"scorer":"sentinel","task_id":"establish-conventions",'
                '"arm":"memory"}'
            ),
            "nomemory-trace": (
                '{"scorer":"verifier","version":"sentinel.v1",'
                '"report_source":"official_harness",'
                f'"run_ref":"{run_ref}"}}'
            ),
        }
    )

    async def visible_linkage(_query: ExperimentQuery) -> LinkageLookup:
        return LinkageLookup(experiment_found=True, linkage_signal="itemCount=1")

    async def skip_sleep(_delay: float) -> None:
        return

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "secret")
    monkeypatch.setattr(gate_cli, "LangfuseClient", lambda *_args: score_client)
    monkeypatch.setattr(gate_cli, "poll_linkage", visible_linkage)
    monkeypatch.setattr(gate_cli.anyio, "sleep", skip_sleep)

    # When
    visible, score_count = await gate_cli._read_back_visibility(
        records,
        links,
        datetime.now(UTC),
        run_ref,
    )

    # Then
    assert visible == (
        "memory-chain-v1.test.memory",
        "memory-chain-v1.test.nomemory",
    )
    assert score_count == 1
