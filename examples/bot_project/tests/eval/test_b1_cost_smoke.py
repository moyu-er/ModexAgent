from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bot.eval.live_gates import b1_cost_runtime, b1_cost_smoke

from modex_agent.core.agent import AgentContext
from modex_agent.core.llm_struct import FinishReason, LLMResponse
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.hook import BeforeGraphHook, HookSpec
from modex_agent.runtime.models import JsonValue
from modex_agent.runtime.services import AgentRuntimeServices
from modex_agent.trace.langfuse_query import ScoreReadData
from modex_agent.trace.score_injector import INJECTOR_VERSION

_TRAJECTORY_NAMES = (
    "tool_success_rate",
    "tool_call_count",
    "error_tool_count",
    "iteration_count",
    "llm_call_count",
    "total_input_tokens",
    "total_output_tokens",
    "total_reasoning_tokens",
    "api_latency_avg_s",
    "cache_hit_rate",
    "response_token_ratio",
    "has_reasoning",
)


def _scores(session_id: str) -> list[ScoreReadData]:
    trajectory_comment = json.dumps(
        {
            "scorer": "trajectory",
            "version": INJECTOR_VERSION,
            "report_source": "counters",
            "run_ref": session_id,
        },
        separators=(",", ":"),
    )
    scores = [
        ScoreReadData(
            name=name,
            value=1.0,
            data_type="NUMERIC",
            comment=trajectory_comment,
        )
        for name in _TRAJECTORY_NAMES
    ]
    scores.append(
        ScoreReadData(
            name="cost_usd",
            value=0.0,
            data_type="NUMERIC",
            comment=json.dumps(
                {
                    "scorer": "pricing",
                    "version": INJECTOR_VERSION,
                    "report_source": "local_pricebook",
                    "run_ref": session_id,
                    "unpriced": ["unknown-model"],
                    "price_source": "model_prices_yml",
                },
                separators=(",", ":"),
            ),
        )
    )
    return scores


async def test_run_gate_writes_green_evidence_for_13_scores_with_unpriced_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    session_id = "b1-smoke.react"
    preflight = b1_cost_smoke.PreflightEvidence(
        langfuse_health=True,
        collector_port=True,
        missing=[],
    )
    dispatch = b1_cost_smoke.TurnDispatch(
        session_id=session_id,
        trace_id="trace-b1",
        output="b1-cost-smoke",
    )
    dispatch_turn = AsyncMock(return_value=dispatch)
    read_scores = AsyncMock(return_value=_scores(session_id))
    monkeypatch.setattr(b1_cost_smoke, "_run_preflight", lambda: preflight)
    monkeypatch.setattr(b1_cost_smoke, "_dispatch_turn", dispatch_turn)
    monkeypatch.setattr(b1_cost_smoke, "_read_trace_scores", read_scores)
    evidence_path = tmp_path / "b1_cost_smoke.json"

    # When
    evidence = await b1_cost_smoke.run_gate(evidence_path=evidence_path)

    # Then
    assert evidence.passed is True
    assert evidence.score_count == 13
    assert evidence.cost_sum_usd == 0.0
    assert evidence.cost_mean_usd == 0.0
    assert evidence.unpriced == ["unknown-model"]
    assert evidence.price_source == "model_prices_yml"
    assert json.loads(evidence_path.read_text(encoding="utf-8")) == evidence.model_dump(
        mode="json"
    )
    dispatch_turn.assert_awaited_once()
    read_scores.assert_awaited_once_with("trace-b1")


async def test_run_gate_stops_after_failed_preflight_and_writes_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    preflight = b1_cost_smoke.PreflightEvidence(
        langfuse_health=True,
        collector_port=False,
        missing=["collector:4318"],
    )
    dispatch_turn = AsyncMock()
    monkeypatch.setattr(b1_cost_smoke, "_run_preflight", lambda: preflight)
    monkeypatch.setattr(b1_cost_smoke, "_dispatch_turn", dispatch_turn)
    evidence_path = tmp_path / "b1_cost_smoke.json"

    # When
    evidence = await b1_cost_smoke.run_gate(evidence_path=evidence_path)

    # Then
    assert evidence.passed is False
    assert evidence.preflight.missing == ["collector:4318"]
    assert evidence.score_count == 0
    assert evidence_path.is_file()
    dispatch_turn.assert_not_awaited()


async def test_dispatch_turn_threads_test_model_to_trace_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    captured: dict[str, str | None] = {}
    build_trace_only_services = b1_cost_runtime.build_trace_only_services

    class ScriptedProvider(CallbackStreamProvider):
        async def chat_stream(
            self,
            messages: list[ChatMessage],
            model: str | None = None,
            temperature: float | None = None,
            max_output_tokens: int | None = None,
            tools: list[dict[str, Any]] | None = None,
            on_content_delta=None,
            on_reasoning_delta=None,
            **kwargs: JsonValue,
        ) -> LLMResponse:
            _ = messages, model, temperature, max_output_tokens, tools, kwargs
            return LLMResponse(
                content="b1-cost-smoke",
                finish_reason=FinishReason.STOP,
                usage={},
            )

        def get_default_model(self) -> str:
            return "openai/step-3.7-flash"

    class ContextPinHook(BeforeGraphHook):
        async def before_graph(self, context: AgentContext) -> None:
            assert context.max_iterations == 1
            assert context.tool_manager.list_tools() == []
            assert context.runtime is not None
            assert context.identity is not None
            assert context.identity.agent_id == "react"

    def fake_build_trace_only_services(
        trace_dir: Path,
        *,
        model: str | None = None,
    ) -> AgentRuntimeServices:
        captured["trace_model"] = model
        services = build_trace_only_services(trace_dir, model=model)
        assert services.hooks is not None
        services.hooks.add(HookSpec(hook=ContextPinHook()))
        return services

    monkeypatch.setenv("TEST_LLM_MODEL", "openai/step-3.7-flash")
    monkeypatch.setenv("TEST_LLM_API_KEY", "test-key")
    monkeypatch.setenv("TEST_LLM_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("OTEL_FORMAT", "file")
    monkeypatch.setattr(
        b1_cost_runtime,
        "create_llm_provider",
        lambda _config: ScriptedProvider(),
    )
    monkeypatch.setattr(
        b1_cost_runtime,
        "build_trace_only_services",
        fake_build_trace_only_services,
    )
    # When
    dispatch = await b1_cost_runtime.dispatch_turn()

    # Then
    assert dispatch.trace_id
    assert captured["trace_model"] == "openai/step-3.7-flash"
