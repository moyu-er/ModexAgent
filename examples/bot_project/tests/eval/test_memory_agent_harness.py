from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from unittest.mock import AsyncMock, MagicMock

import bot.eval.agent_harness as agent_harness
import bot.eval.memory_harness as memory_harness
import pytest
from bot.eval.agent_harness import (
    build_memory_runtime_services,
    run_dream_until_exhausted,
)

from modex_agent.core.emitter import AgentResult
from modex_agent.core.message import ChatMessage
from modex_agent.core.provider import CallbackStreamProvider
from modex_agent.core.scope import MemoryContext
from modex_agent.core.types import LLMResponse
from modex_agent.ioc.configs.observability import ObservabilityConfig, TraceBackend
from modex_agent.memory.default_system import DefaultMemorySystem, ScopedMessageHistory
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.langfuse_query import Provenance
from modex_agent.trace.memory_trace_hook import MemoryTelemetryCounters, MemoryTraceHook
from modex_agent.trace.score_injector import L2ScoreInjector

_SESSION_ID: Final = "eval.memory.react"


class _NoCallProvider(CallbackStreamProvider):
    async def chat_stream(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        on_content_delta=None,
        on_reasoning_delta=None,
        **kwargs: Any,
    ) -> LLMResponse:
        del messages, model, temperature, max_output_tokens, tools, kwargs
        raise AssertionError("assembly must not call the provider")

    def get_default_model(self) -> str:
        return "scripted-memory-model"


class _DreamProviderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _Record:
    context: MemoryContext


async def test_build_memory_runtime_services_assembles_real_memory_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.setenv("OTEL_FORMAT", "file")

    # When
    bundle = await build_memory_runtime_services(tmp_path, _NoCallProvider())
    context_state = await bundle.context_manager.load(_SESSION_ID)

    # Then
    assert isinstance(bundle.memory_system, DefaultMemorySystem)
    # declared in config/scopes/eval/agents/memory-harness.yml
    assert bundle.memory_config.session.max_context_tokens == 32_000
    assert bundle.memory_config.archive is not None
    assert bundle.memory_config.archive.enabled is True
    assert bundle.memory_config.core is not None
    assert bundle.memory_config.core.enabled is True
    assert bundle.memory_config.dream_engine is not None
    assert bundle.memory_config.dream_engine.enabled is True
    assert bundle.memory_system.archive_manager is not None
    assert bundle.memory_system.core_memory_manager is not None
    assert bundle.memory_system.core_memory_consolidator is not None
    assert isinstance(context_state.history, ScopedMessageHistory)
    assert isinstance(bundle.memory_trace_hook, MemoryTraceHook)
    assert bundle.memory_trace_hook in bundle.memory_system.hook_runner._hooks
    assert bundle.memory_trace_hook._store is bundle.runtime_services.trace_store

    await bundle.assembly.close()


async def test_memory_counter_score_hook_publishes_nonzero_session_counters() -> None:
    # Given
    memory_trace_hook = MagicMock(spec=MemoryTraceHook)
    memory_trace_hook.read_counters.return_value = MemoryTelemetryCounters(
        memory_cleanup_total=2,
        memory_context_assembled_total=1,
    )
    score_injector = MagicMock(spec=L2ScoreInjector)
    score_injector.inject_score_batch = AsyncMock()
    hook = memory_harness._MemoryCounterScoreHook(memory_trace_hook, score_injector)
    context = MagicMock()
    context.session.session_id = _SESSION_ID
    context.runtime.state.custom = {
        TurnCustomKey.TRACE_ID: "trace-memory-counters",
        TurnCustomKey.ROOT_SPAN_ID: "root-memory-counters",
    }

    # When
    await hook.on_outcome(context, AgentResult(content="done"))

    # Then
    score_injector.inject_score_batch.assert_awaited_once()
    trace_id, scores = score_injector.inject_score_batch.await_args.args
    assert trace_id == "trace-memory-counters"
    assert [(score.name, score.value, score.data_type) for score in scores] == [
        ("memory_cleanup_total", 2.0, "NUMERIC"),
        ("memory_context_assembled_total", 1.0, "NUMERIC"),
    ]
    assert all(
        Provenance.model_validate_json(score.comment)
        == Provenance(
            scorer="verifier",
            version="memory_trace.v1",
            report_source="counters",
            run_ref=_SESSION_ID,
        )
        for score in scores
    )
    assert score_injector.inject_score_batch.await_args.kwargs == {
        "observation_id": "root-memory-counters"
    }


async def test_memory_counter_score_hook_is_silent_for_zero_counters() -> None:
    # Given
    memory_trace_hook = MagicMock(spec=MemoryTraceHook)
    memory_trace_hook.read_counters.return_value = MemoryTelemetryCounters()
    score_injector = MagicMock(spec=L2ScoreInjector)
    score_injector.inject_score_batch = AsyncMock()
    hook = memory_harness._MemoryCounterScoreHook(memory_trace_hook, score_injector)
    context = MagicMock()
    context.session.session_id = _SESSION_ID
    context.runtime.state.custom = {
        TurnCustomKey.TRACE_ID: "trace-zero-counters",
        TurnCustomKey.ROOT_SPAN_ID: "root-zero-counters",
    }

    # When
    await hook.on_outcome(context, AgentResult(content="done"))

    # Then
    score_injector.inject_score_batch.assert_not_awaited()


async def test_build_memory_runtime_services_reuses_trace_score_injector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    score_injector = MagicMock(spec=L2ScoreInjector)
    monkeypatch.setattr(
        agent_harness,
        "_eval_observability",
        lambda: (
            ObservabilityConfig(trace_backend=TraceBackend.FILE),
            score_injector,
        ),
    )

    # When
    bundle = await build_memory_runtime_services(tmp_path, _NoCallProvider())

    # Then
    assert bundle.runtime_services.hooks is not None
    counter_hooks = [
        spec.hook
        for spec in bundle.runtime_services.hooks.hook_specs
        if isinstance(spec.hook, memory_harness._MemoryCounterScoreHook)
    ]
    assert len(counter_hooks) == 1
    assert counter_hooks[0]._score_injector is score_injector

    await bundle.runtime_services.hooks.aclose()
    await bundle.assembly.close()


async def test_run_dream_until_exhausted_reports_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    memory_system, engine = _dream_fixture(monkeypatch, counts=[2, 0])

    # When
    summary = await run_dream_until_exhausted(memory_system)

    # Then
    assert summary.iterations == 1
    assert summary.exhausted is True
    assert summary.stalled is False
    engine.run.assert_awaited_once()


async def test_run_dream_until_exhausted_reports_stall_without_cursor_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    memory_system, engine = _dream_fixture(monkeypatch, counts=[2, 2])

    # When
    summary = await run_dream_until_exhausted(memory_system)

    # Then
    assert summary.iterations == 1
    assert summary.exhausted is False
    assert summary.stalled is True
    engine.run.assert_awaited_once()


async def test_run_dream_until_exhausted_records_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    memory_system, engine = _dream_fixture(monkeypatch, counts=[2])
    engine.run.side_effect = _DreamProviderError("provider offline")

    # When
    summary = await run_dream_until_exhausted(memory_system)

    # Then
    assert summary.iterations == 1
    assert summary.exhausted is False
    assert summary.stalled is False
    assert "Dream consolidation failed" in caplog.text


async def test_run_dream_until_exhausted_stops_at_iteration_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    memory_system, engine = _dream_fixture(monkeypatch, counts=[5, 4, 3, 2])

    # When
    summary = await run_dream_until_exhausted(memory_system, max_iterations=3)

    # Then
    assert summary.iterations == 3
    assert summary.exhausted is False
    assert summary.stalled is False
    assert engine.run.await_count == 3


def _dream_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    counts: list[int],
) -> tuple[MagicMock, MagicMock]:
    context = MemoryContext(
        session_id=_SESSION_ID,
        user_id="memory-user",
        agent_id="react",
    )
    registry = MagicMock()
    registry.list_records = AsyncMock(return_value=[_Record(context=context)])
    memory_system = MagicMock(spec=DefaultMemorySystem)
    memory_system.archive_manager = MagicMock()
    memory_system.core_memory_manager = MagicMock()
    memory_system.core_memory_consolidator = MagicMock()
    memory_system.store_registry = registry
    memory_system.hook_runner = MagicMock()
    memory_system.get_unprocessed_history_count = AsyncMock(side_effect=counts)
    engine = MagicMock()
    engine.run = AsyncMock(return_value=False)
    monkeypatch.setattr(memory_harness, "DreamEngine", MagicMock(return_value=engine))
    return memory_system, engine
