from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from bot.eval.dataset_curator import DatasetCurator
from bot.eval.judge_pass import ExperimentWindow

from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.runtime.models import JsonValue
from modex_agent.trace.langfuse_query import LangfuseClient, ObservationData
from modex_agent.trace.score_injector import L2ScoreInjector, ScoreSpec


class ScriptedJudgeProvider(CallbackStreamProvider):
    """Mutable deterministic provider used to exercise the real judge runner."""

    def __init__(self, responses: Sequence[str]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    def get_default_model(self) -> str:
        return "judge/test-model"

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, JsonValue]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        seed: int | None = None,
        **kwargs: JsonValue,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, seed, kwargs
        self.calls += 1
        return LLMResponse(
            content=self._responses.pop(0),
            finish_reason=FinishReason.STOP,
        )


class ScriptedObservationClient(LangfuseClient):
    """In-memory observation pages with no network client."""

    def __init__(self, pages: Sequence[list[ObservationData]]) -> None:
        self._pages = list(pages)
        self.calls = 0
        self.closed = False

    async def get_observations(
        self,
        *,
        session_id: str | None = None,
        trace_id: str | None = None,
        from_start_time: datetime | None = None,
        to_start_time: datetime | None = None,
        cursor: str | None = None,
        limit: int = 500,
    ) -> tuple[list[ObservationData], str | None]:
        del session_id, trace_id, from_start_time, to_start_time, cursor, limit
        page = self._pages[self.calls]
        self.calls += 1
        next_cursor = "next" if self.calls < len(self._pages) else None
        return page, next_cursor

    async def close(self) -> None:
        self.closed = True


class ScriptedCurator(DatasetCurator):
    """Trace-I/O source that preserves the curator's production seam."""

    def __init__(
        self,
        trace_io: Mapping[str, Mapping[str, JsonValue] | None],
    ) -> None:
        self._trace_io = {
            trace_id: dict(payload) if payload is not None else None
            for trace_id, payload in trace_io.items()
        }

    async def fetch_trace_io(self, trace_id: str) -> dict[str, JsonValue] | None:
        return self._trace_io[trace_id]


class RecordingInjector(L2ScoreInjector):
    """Captures typed score batches without sending ingestion events."""

    def __init__(self) -> None:
        self.batches: list[tuple[str, list[ScoreSpec]]] = []
        self.closed = False

    async def inject_score_batch(
        self,
        trace_id: str,
        scores: list[ScoreSpec],
        *,
        observation_id: str | None = None,
    ) -> None:
        del observation_id
        self.batches.append((trace_id, scores))

    async def aclose(self) -> None:
        self.closed = True


def observation(
    trace_id: str,
    *,
    parent_id: str | None = None,
    observation_type: str = "AGENT",
) -> ObservationData:
    started = datetime(2026, 8, 20, tzinfo=UTC)
    return ObservationData(
        id=f"obs-{trace_id}",
        trace_id=trace_id,
        start_time=started,
        end_time=started + timedelta(seconds=1),
        parent_observation_id=parent_id,
        type=observation_type,
        name="invoke_agent",
        level="DEFAULT",
        input="input",
        output="output",
        usage_details=None,
        metadata=None,
        provided_model_name=None,
        session_id=f"session-{trace_id}",
        latency=1.0,
        status_message=None,
    )


def verdict_json(evidence: str, *, completion: str = "MET") -> str:
    return json.dumps(
        {
            "verdicts": [
                {"criterion": "task_completion", "verdict": completion, "evidence": evidence},
                {
                    "criterion": "empirical_verification",
                    "verdict": "UNMET",
                    "evidence": "No verification output.",
                },
                {
                    "criterion": "instruction_following",
                    "verdict": "MET",
                    "evidence": "Instructions followed.",
                },
                {
                    "criterion": "grounded_reporting",
                    "verdict": "NA",
                    "evidence": "Not applicable.",
                },
                {
                    "criterion": "efficiency",
                    "verdict": "MET",
                    "evidence": "Direct execution.",
                },
            ],
            "summary": "Scripted summary.",
        }
    )


def experiment() -> ExperimentWindow:
    return ExperimentWindow.model_validate(
        {
            "name": "baseline-v1",
            "datasetId": "dataset-1",
            "startTime": datetime(2026, 8, 20, tzinfo=UTC),
            "endTime": datetime(2026, 8, 21, tzinfo=UTC),
        }
    )
