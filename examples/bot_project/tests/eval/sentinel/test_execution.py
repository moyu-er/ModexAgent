from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from bot.eval.sentinel.execution import HostSentinelExecutionPlane, evaluate_observation
from bot.eval.sentinel.results import SentinelTaskStatus
from evals.sentinel.tasks import MEMORY_CHAIN_V1_CHAIN, SentinelArm

from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import LLMProvider
from modex_agent.core.types import LLMResponse
from modex_agent.ioc.configs.observability import TraceBackend
from modex_agent.runtime.models import JsonValue
from modex_agent.trace.experiment_attrs import ExperimentAttribute, ExperimentLinkage
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.score_injector import L2ScoreInjector


class _NoCallProvider(LLMProvider):
    async def chat(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: list[dict[str, JsonValue]] | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        _ = (messages, model, temperature, max_output_tokens, tools, kwargs)
        raise AssertionError("provider must not be called")

    def get_default_model(self) -> str:
        return "scripted"


def test_evaluate_observation_reads_real_world_and_memory_evidence(tmp_path: Path) -> None:
    # Given
    task = MEMORY_CHAIN_V1_CHAIN.tasks[1]
    report = tmp_path / "reports" / "status.md"
    report.parent.mkdir(parents=True)
    report.write_text("Work is stable. Delivery is ready.\nBLUEHERON\n", encoding="utf-8")

    # When
    observation = evaluate_observation(task, tmp_path, "Created the BLUEHERON report.")

    # Then
    assert observation.status is SentinelTaskStatus.SUCCESS
    assert all(item.passed for item in observation.world_assertions)
    assert all(item.passed for item in observation.memory_assertions)


def test_evaluate_observation_fails_when_recalled_fact_is_absent(tmp_path: Path) -> None:
    # Given
    task = MEMORY_CHAIN_V1_CHAIN.tasks[1]
    report = tmp_path / "reports" / "status.md"
    report.parent.mkdir(parents=True)
    report.write_text("Work is stable. Delivery is ready.\nUNKNOWN\n", encoding="utf-8")

    # When
    observation = evaluate_observation(task, tmp_path, "Created the status report.")

    # Then
    assert observation.status is SentinelTaskStatus.FAILED
    assert observation.memory_assertions[0].passed is False


async def test_host_execution_emits_all_five_experiment_attributes(tmp_path: Path) -> None:
    # Given
    linkage = ExperimentLinkage(
        experiment_id="experiment-id",
        experiment_name="memory-chain-v1.run.memory",
        dataset_id="dataset-id",
        item_id="item-id",
    )
    execution = HostSentinelExecutionPlane(
        _NoCallProvider(),
        lambda _instance: linkage,
        run_ref="evals/runs/sentinel/run",
    )
    store = OtelSpanTraceStore(tmp_path, backend=TraceBackend.FILE)

    # When
    record = await execution._emit_task_span(
        store,
        linkage,
        "sentinel.session",
        arm=SentinelArm.MEMORY,
        task_id="establish-conventions",
        succeeded=True,
    )
    spans = await store.list_by_trace_id(record.trace_id)

    # Then
    assert len(spans) == 1
    attributes = spans[0].attributes
    assert attributes[ExperimentAttribute.ID] == "experiment-id"
    assert attributes[ExperimentAttribute.NAME] == "memory-chain-v1.run.memory"
    assert attributes[ExperimentAttribute.DATASET_ID] == "dataset-id"
    assert attributes[ExperimentAttribute.ITEM_ID] == "item-id"
    assert attributes[ExperimentAttribute.ITEM_ROOT_OBSERVATION_ID] == spans[0].span_id


async def test_host_execution_injects_task_verdict_on_emitted_trace(tmp_path: Path) -> None:
    # Given
    linkage = ExperimentLinkage(
        experiment_id="experiment-id",
        experiment_name="memory-chain-v1.run.memory",
        dataset_id="dataset-id",
        item_id="item-id",
    )
    injector = AsyncMock(spec=L2ScoreInjector)
    execution = HostSentinelExecutionPlane(
        _NoCallProvider(),
        lambda _instance: linkage,
        run_ref="evals/runs/sentinel/run",
        score_injector=injector,
    )
    store = OtelSpanTraceStore(tmp_path, backend=TraceBackend.FILE)

    # When
    record = await execution._emit_task_span(
        store,
        linkage,
        "sentinel.session",
        arm=SentinelArm.MEMORY,
        task_id="establish-conventions",
        succeeded=True,
    )

    # Then
    assert record.task_id == "establish-conventions"
    injector.inject_score_batch.assert_awaited_once()
    score = injector.inject_score_batch.await_args.args[1][0]
    assert score.name == "verdict_memory_chain_v1"
    assert score.value is True
    assert json.loads(score.comment or "") == {
        "scorer": "verifier",
        "version": "sentinel.v1",
        "report_source": "official_harness",
        "run_ref": "evals/runs/sentinel/run",
    }
