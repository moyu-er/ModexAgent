"""RootSpanHook L2 score injection over the session-state metric counters.

Ticket 3 of the trace write-only refactor: ``finally_graph`` derives
``TrajectoryMetrics`` from ``TraceSessionState.read_metrics`` (never reads
spans back from the store), stashes them on the turn-scoped carrier
(``TurnCustomKey.TRAJECTORY_METRICS``) before ``clear_trace``, and injects
them fire-and-forget. These tests lock the counter path — including the
hook-level parity sentinel (injected metrics == ``compute_metrics`` over the
spans the store saw) and the stash lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modex_agent.agents.react.state import ReActTurnState
from modex_agent.core.agent import AgentContext
from modex_agent.core.emitter import AgentResult, StopReason
from modex_agent.core.session_id import SessionInfo
from modex_agent.memory.history import ListMessageHistory
from modex_agent.runtime.enums import AgentKind, TurnCustomKey, TurnPhase
from modex_agent.runtime.models import TurnIdentity
from modex_agent.runtime.services import AgentRuntime, AgentRuntimeServices
from modex_agent.tools.manager import InMemoryToolManager
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.pricing import PriceBook, PriceEntry
from modex_agent.trace.root_span_hook import RootSpanHook
from modex_agent.trace.score_injector import INJECTOR_VERSION, L2ScoreInjector, ScoreSpec
from modex_agent.trace.scoring import TrajectoryMetrics, compute_metrics
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName, SpanStatusCode
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.store import SpanModel, SpanStatus

_TRACE_ID = "shared-trace"

_LOGGER_NAME = "modex_agent.trace.root_span_hook"
_TRAJECTORY_SCORE_NAMES = frozenset(
    {
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
    }
)


async def _drain_pending(hook: RootSpanHook) -> None:
    """Wait for the hook's fire-and-forget injection tasks to finish."""
    await asyncio.gather(*hook._pending_injections)


def _make_context(
    trace_id: str,
    root_span_id: str,
    parent_span_id: str | None = None,
) -> AgentContext:
    session = SessionInfo(
        session_id=f"session.{root_span_id}",
        agent_name=root_span_id,
        parent_session_id="session.parent" if parent_span_id is not None else None,
    )
    state = ReActTurnState(
        identity=TurnIdentity(agent_id=root_span_id, session=session, turn_id="turn-1"),
        agent_kind=AgentKind.REACT,
        phase=TurnPhase.CREATED,
    )
    state.custom[TurnCustomKey.TRACE_ID] = trace_id
    state.custom[TurnCustomKey.ROOT_SPAN_ID] = root_span_id
    if parent_span_id is not None:
        state.custom[TurnCustomKey.PARENT_SPAN_ID] = parent_span_id
    return AgentContext(
        system_prompt="test",
        history=ListMessageHistory(),
        tool_manager=InMemoryToolManager(),
        session=session,
        runtime=AgentRuntime(services=AgentRuntimeServices(), state=state),
    )


def _assert_default_cost_kwargs(kwargs: dict[str, Any], root_span_id: str) -> None:
    assert kwargs["observation_id"] == root_span_id
    assert kwargs["session_id"] == f"session.{root_span_id}"
    extra_scores: list[ScoreSpec] = kwargs["extra_scores"]
    assert len(extra_scores) == 1
    assert extra_scores[0].name == "cost_usd"
    assert extra_scores[0].value == 0.0
    assert json.loads(extra_scores[0].comment or "") == {
        "scorer": "pricing",
        "version": INJECTOR_VERSION,
        "report_source": "local_pricebook",
        "run_ref": f"session.{root_span_id}",
        "unpriced": [],
        "price_source": "prices_json",
    }


def _span(
    span_id: str,
    parent_span_id: str | None,
    name: SpanName,
    *,
    attributes: dict[str, Any] | None = None,
    status: SpanStatus | None = None,
    start: float = 1.0,
    end: float | None = 2.0,
) -> SpanModel:
    return SpanModel(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=parent_span_id,
        name=name.value,
        kind=SpanKind.INTERNAL.value,
        start_time=start,
        end_time=end,
        attributes=attributes or {},
        status=status or SpanStatus(),
    )


def _seed_turn_spans(session: TraceSessionState, root_span_id: str, spans: list[SpanModel]) -> None:
    """Populate the counters exactly as ``_save_span`` accumulation would."""
    for span in spans:
        session.accumulate_span(_TRACE_ID, root_span_id, span)


async def test_single_root_injects_score_for_only_its_metrics() -> None:
    root_id = "root"
    spans = [
        _span(
            "chat",
            root_id,
            SpanName.CHAT,
            attributes={
                GenAiAttr.USAGE_REASONING_TOKENS.value: 12,
                GenAiAttr.USAGE_INPUT_TOKENS.value: 20,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 10,
            },
        ),
        _span("tool", "chat", SpanName.EXECUTE_TOOL),
    ]
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    _seed_turn_spans(session, root_id, spans)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_id),
        result=AgentResult(content="done"),
    )
    await _drain_pending(hook)

    injector.inject_scores.assert_awaited_once()
    await_args = injector.inject_scores.await_args
    assert await_args.args == (_TRACE_ID, compute_metrics(spans))
    _assert_default_cost_kwargs(await_args.kwargs, root_id)
    store.list_by_trace_id.assert_not_awaited()


async def test_completed_turn_injects_cost_with_session_provenance_and_lazy_pricebook() -> None:
    # Given
    root_id = "priced-root"
    spans = [
        _span(
            "chat",
            root_id,
            SpanName.CHAT,
            attributes={
                GenAiAttr.RESPONSE_MODEL.value: "priced-model",
                GenAiAttr.USAGE_INPUT_TOKENS.value: 1_000_000,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 500_000,
            },
        )
    ]
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    _seed_turn_spans(session, root_id, spans)
    pricebook = PriceBook(
        models={
            "priced-model": PriceEntry(
                input=1.0,
                output=2.0,
                cache_read=0.1,
                cache_write=1.25,
            )
        }
    )
    override_path = Path("model_prices.yml")

    with patch(
        "modex_agent.trace.root_span_hook.load_pricebook",
        return_value=pricebook,
    ) as load_pricebook:
        hook = RootSpanHook(
            session=session,
            store=store,
            score_injector=injector,
            pricebook_yml_path=override_path,
        )

        # When
        await hook.finally_graph(
            _make_context(_TRACE_ID, root_id),
            result=AgentResult(content="done"),
        )
        await _drain_pending(hook)

    # Then
    load_pricebook.assert_called_once_with(yml_path=override_path)
    await_args = injector.inject_scores.await_args
    assert await_args.kwargs["session_id"] == f"session.{root_id}"
    extra_scores = await_args.kwargs["extra_scores"]
    assert extra_scores == [
        ScoreSpec(
            name="cost_usd",
            value=2.0,
            data_type="NUMERIC",
            comment=extra_scores[0].comment,
        )
    ]
    assert json.loads(extra_scores[0].comment or "") == {
        "scorer": "pricing",
        "version": INJECTOR_VERSION,
        "report_source": "local_pricebook",
        "run_ref": f"session.{root_id}",
        "unpriced": [],
        "price_source": "model_prices_yml",
    }


@patch("modex_agent.trace.score_injector.httpx.AsyncClient")
async def test_pricebook_failure_injects_trajectory_scores_without_breaking_turn(
    mock_client_cls: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    root_id = "corrupt-pricebook-root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    mock_client = AsyncMock()
    mock_client.is_closed = False
    mock_client.aclose = AsyncMock()
    mock_client.post = AsyncMock(
        return_value=MagicMock(
            status_code=207,
            json=lambda: {"successes": [], "errors": []},
        )
    )
    mock_client_cls.return_value = mock_client
    injector = L2ScoreInjector(
        ingestion_url="https://ingest.example.invalid/api/public/ingestion",
        headers={"Authorization": "Basic test"},
    )
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    # When
    with (
        patch(
            "modex_agent.trace.root_span_hook.load_pricebook",
            side_effect=RuntimeError("corrupted builtin prices.json"),
        ),
        caplog.at_level(logging.WARNING, logger=_LOGGER_NAME),
    ):
        await hook.finally_graph(
            _make_context(_TRACE_ID, root_id),
            result=AgentResult(content="done"),
        )
        await _drain_pending(hook)

    # Then
    mock_client.post.assert_awaited_once()
    batch = mock_client.post.await_args.kwargs["json"]["batch"]
    score_names = {event["body"]["name"] for event in batch}
    assert len(batch) == 12
    assert score_names == _TRAJECTORY_SCORE_NAMES
    assert "Root trace cost score computation failed" in caplog.text


async def test_shared_trace_injects_each_root_without_cross_contamination() -> None:
    parent_root = "parent-root"
    parent_spans = [
        _span(
            "parent-chat",
            parent_root,
            SpanName.CHAT,
            attributes={
                GenAiAttr.USAGE_REASONING_TOKENS.value: 10,
                GenAiAttr.USAGE_INPUT_TOKENS.value: 80,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 20,
            },
        ),
        _span("parent-tool", "parent-chat", SpanName.EXECUTE_TOOL),
    ]
    child_root = "child-root"
    child_spans = [
        _span(
            "child-chat",
            child_root,
            SpanName.CHAT,
            attributes={
                GenAiAttr.USAGE_REASONING_TOKENS.value: 200,
                GenAiAttr.USAGE_INPUT_TOKENS.value: 40,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 10,
            },
        ),
        _span(
            "child-tool",
            "child-chat",
            SpanName.EXECUTE_TOOL,
            status=SpanStatus(code=SpanStatusCode.ERROR),
        ),
    ]
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    # Parent and child run separate build_trace_hooks() calls → separate
    # session states; they share only the trace_id.
    parent_session = TraceSessionState()
    parent_session.root_span_info[_TRACE_ID] = (parent_root, 1.0)
    _seed_turn_spans(parent_session, parent_root, parent_spans)
    child_session = TraceSessionState()
    child_session.root_span_info[_TRACE_ID] = (child_root, 1.0)
    _seed_turn_spans(child_session, child_root, child_spans)
    parent_hook = RootSpanHook(session=parent_session, store=store, score_injector=injector)
    child_hook = RootSpanHook(session=child_session, store=store, score_injector=injector)

    await parent_hook.finally_graph(
        _make_context(_TRACE_ID, parent_root),
        result=AgentResult(content="parent"),
    )
    await child_hook.finally_graph(
        _make_context(_TRACE_ID, child_root, "handoff"),
        result=AgentResult(content="child"),
    )
    await _drain_pending(parent_hook)
    await _drain_pending(child_hook)

    assert injector.inject_scores.await_count == 2
    first = injector.inject_scores.await_args_list[0]
    second = injector.inject_scores.await_args_list[1]
    assert first.args == (_TRACE_ID, compute_metrics(parent_spans))
    _assert_default_cost_kwargs(first.kwargs, parent_root)
    assert second.args == (_TRACE_ID, compute_metrics(child_spans))
    _assert_default_cost_kwargs(second.kwargs, child_root)


async def test_turn_without_accumulated_spans_injects_zero_metrics() -> None:
    """read_metrics yields the zero shape when no accumulating span exists."""
    root_id = "root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_id),
        result=AgentResult(content="done"),
    )
    await _drain_pending(hook)

    injector.inject_scores.assert_awaited_once()
    await_args = injector.inject_scores.await_args
    assert await_args.args == (_TRACE_ID, compute_metrics([]))
    _assert_default_cost_kwargs(await_args.kwargs, root_id)


async def test_injector_failure_does_not_escape_finally_graph() -> None:
    root_id = "root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    injector.inject_scores.side_effect = RuntimeError("injection failed")
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_id),
        result=AgentResult(content="done"),
    )
    await _drain_pending(hook)


async def test_no_injector_performs_no_score_query_work() -> None:
    root_id = "root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=None)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_id),
        result=AgentResult(content="done"),
    )

    store.list_by_trace_id.assert_not_awaited()


async def test_non_completed_root_skips_score_injection() -> None:
    root_id = "root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    _seed_turn_spans(session, root_id, [_span("tool", root_id, SpanName.EXECUTE_TOOL)])
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    await hook.finally_graph(
        _make_context(_TRACE_ID, root_id),
        result=AgentResult(content="failed", stop_reason=StopReason.ERROR),
    )

    store.list_by_trace_id.assert_not_awaited()
    injector.inject_scores.assert_not_awaited()


# ── hook-level parity sentinel: counters vs compute_metrics ────────────


async def test_injected_metrics_equal_compute_metrics_over_saved_spans() -> None:
    """Drive a synthetic multi-span turn through the real _save_span path
    (chat with usage + tool error + iterations + no-op kinds + a root span
    carrying CUMULATIVE usage) and assert the injected metrics equal
    ``compute_metrics`` over exactly the spans the store saw."""
    root_id = "root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)
    ctx = _make_context(_TRACE_ID, root_id)

    async def save(
        span_id: str,
        name: SpanName,
        *,
        attributes: dict[str, Any] | None = None,
        status: SpanStatus | None = None,
        start: float = 1.0,
        end: float | None = 2.0,
    ) -> None:
        await hook._save_span(
            trace_id=_TRACE_ID,
            span_id=span_id,
            parent_span_id=root_id,
            name=name.value,
            kind=SpanKind.INTERNAL.value,
            start_time=start,
            end_time=end,
            attributes=attributes or {},
            status=status or SpanStatus(),
            ctx=ctx,
        )

    await save("iter-1", SpanName.ITERATION_START)
    await save(
        "chat-1",
        SpanName.CHAT,
        start=1.0,
        end=2.5,
        attributes={
            GenAiAttr.USAGE_INPUT_TOKENS.value: 200,
            GenAiAttr.USAGE_OUTPUT_TOKENS.value: 100,
            GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: 60,
        },
    )
    await save("tool-1", SpanName.EXECUTE_TOOL)
    await save(
        "tool-2",
        SpanName.EXECUTE_TOOL,
        status=SpanStatus(code=SpanStatusCode.ERROR),
    )
    await save("iter-2", SpanName.ITERATION_START)
    await save(
        "chat-2",
        SpanName.CHAT,
        start=3.0,
        end=5.5,
        attributes={
            GenAiAttr.USAGE_INPUT_TOKENS.value: 250,
            GenAiAttr.USAGE_OUTPUT_TOKENS.value: 150,
            GenAiAttr.USAGE_REASONING_TOKENS.value: 90,
        },
    )
    await save("handoff", SpanName.AGENT_HANDOFF)

    # finally_graph writes the root span itself, with cumulative turn usage.
    session.turn_usage[_TRACE_ID] = {"input_tokens": 450, "output_tokens": 250}
    await hook.finally_graph(ctx, result=AgentResult(content="done"))
    await _drain_pending(hook)

    saved_spans = [c.args[0] for c in store.save_span.call_args_list]
    assert any(s.name == SpanName.INVOKE_AGENT.value for s in saved_spans)
    injector.inject_scores.assert_awaited_once()
    await_args = injector.inject_scores.await_args
    injected = await_args.args[1]
    assert isinstance(injected, TrajectoryMetrics)
    assert await_args.args[0] == _TRACE_ID
    _assert_default_cost_kwargs(await_args.kwargs, root_id)
    expected = compute_metrics(saved_spans)
    for field in TrajectoryMetrics.model_fields:
        assert getattr(injected, field) == getattr(expected, field), (
            f"parity drift on {field}: counters={getattr(injected, field)!r} "
            f"compute_metrics={getattr(expected, field)!r}"
        )


# ── stash: metrics survive clear_trace on the turn carrier ──────────────


async def test_finally_graph_stashes_metrics_before_clear_trace() -> None:
    root_id = "root"
    spans = [
        _span(
            "chat",
            root_id,
            SpanName.CHAT,
            attributes={
                GenAiAttr.USAGE_INPUT_TOKENS.value: 30,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 12,
            },
        ),
        _span(
            "tool",
            root_id,
            SpanName.EXECUTE_TOOL,
            status=SpanStatus(code=SpanStatusCode.ERROR),
        ),
    ]
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    _seed_turn_spans(session, root_id, spans)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)
    ctx = _make_context(_TRACE_ID, root_id)

    await hook.finally_graph(ctx, result=AgentResult(content="done"))
    await _drain_pending(hook)

    assert ctx.runtime is not None
    stashed = ctx.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]
    assert isinstance(stashed, TrajectoryMetrics)
    assert stashed == compute_metrics(spans)
    # The counters bucket is gone, but the stash survives.
    assert session.read_metrics(_TRACE_ID, root_id) == compute_metrics([])
    assert stashed == compute_metrics(spans)


async def test_non_completed_turn_stashes_metrics_but_skips_injection() -> None:
    root_id = "root"
    spans = [
        _span(
            "tool",
            root_id,
            SpanName.EXECUTE_TOOL,
            status=SpanStatus(code=SpanStatusCode.ERROR),
        ),
    ]
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    _seed_turn_spans(session, root_id, spans)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)
    ctx = _make_context(_TRACE_ID, root_id)

    await hook.finally_graph(
        ctx,
        result=AgentResult(content="failed", stop_reason=StopReason.ERROR),
    )

    injector.inject_scores.assert_not_awaited()
    assert ctx.runtime is not None
    stashed = ctx.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]
    assert isinstance(stashed, TrajectoryMetrics)
    assert stashed == compute_metrics(spans)


# ── fire-and-forget: telemetry must not stall turn teardown ──────────


async def test_finally_graph_returns_promptly_when_injector_hangs() -> None:
    """A hung Langfuse injection (5 s timeout path) must not delay finally_graph."""
    root_id = "root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)

    async def _hang_injection(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(30)

    injector.inject_scores.side_effect = _hang_injection
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    await asyncio.wait_for(
        hook.finally_graph(
            _make_context(_TRACE_ID, root_id),
            result=AgentResult(content="done"),
        ),
        timeout=0.5,
    )

    assert hook._pending_injections
    for task in hook._pending_injections:
        task.cancel()
    await asyncio.gather(*hook._pending_injections, return_exceptions=True)
    await asyncio.sleep(0.05)
    assert not hook._pending_injections


async def test_injector_exception_swallowed_in_background_task(
    caplog: pytest.LogCaptureFixture,
) -> None:
    root_id = "root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    injector.inject_scores.side_effect = RuntimeError("injection failed")
    session = TraceSessionState()
    session.root_span_info[_TRACE_ID] = (root_id, 1.0)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    with caplog.at_level(logging.WARNING, logger=_LOGGER_NAME):
        await hook.finally_graph(
            _make_context(_TRACE_ID, root_id),
            result=AgentResult(content="done"),
        )
        await _drain_pending(hook)

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and "Root trace score injection failed" in record.getMessage()
    ]
    assert warnings


# ── approval-resume: root resolved from turn state, not root_span_info ─────


async def test_resumed_turn_emits_root_span_stash_and_injection() -> None:
    """Approval-resume skips START_NODE_TURN and TraceSessionState is never
    snapshotted, so root_span_info has no entry — finally_graph must fall
    back to custom[ROOT_SPAN_ID] + state.created_at and complete the whole
    path: root span emission, stash, injection, clear_trace."""
    root_id = "resumed-root"
    spans = [
        _span(
            "chat",
            root_id,
            SpanName.CHAT,
            attributes={
                GenAiAttr.USAGE_INPUT_TOKENS.value: 60,
                GenAiAttr.USAGE_OUTPUT_TOKENS.value: 25,
            },
        ),
        _span(
            "tool",
            root_id,
            SpanName.EXECUTE_TOOL,
            status=SpanStatus(code=SpanStatusCode.ERROR),
        ),
    ]
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    _seed_turn_spans(session, root_id, spans)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)
    ctx = _make_context(_TRACE_ID, root_id)
    assert ctx.runtime is not None
    ctx.runtime.state.created_at = 1000.5

    await hook.finally_graph(ctx, result=AgentResult(content="resumed"))
    await _drain_pending(hook)

    saved = [c.args[0] for c in store.save_span.call_args_list]
    assert len(saved) == 1
    root_span = saved[0]
    assert root_span.span_id == root_id
    assert root_span.name == SpanName.INVOKE_AGENT.value
    assert root_span.start_time == 1000.5
    injector.inject_scores.assert_awaited_once()
    await_args = injector.inject_scores.await_args
    assert await_args.args == (_TRACE_ID, compute_metrics(spans))
    _assert_default_cost_kwargs(await_args.kwargs, root_id)
    stashed = ctx.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]
    assert stashed == compute_metrics(spans)
    assert _TRACE_ID not in session._metric_counters


async def test_resumed_turn_non_completed_stashes_and_clears_without_injection() -> None:
    root_id = "resumed-root"
    spans = [_span("tool", root_id, SpanName.EXECUTE_TOOL)]
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    _seed_turn_spans(session, root_id, spans)
    hook = RootSpanHook(session=session, store=store, score_injector=injector)
    ctx = _make_context(_TRACE_ID, root_id)

    await hook.finally_graph(
        ctx,
        result=AgentResult(content="failed", stop_reason=StopReason.ERROR),
    )

    injector.inject_scores.assert_not_awaited()
    assert ctx.runtime is not None
    stashed = ctx.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]
    assert stashed == compute_metrics(spans)
    assert _TRACE_ID not in session._metric_counters


async def test_missing_root_everywhere_clears_counters_then_raises() -> None:
    """Leak backstop: with neither root_span_info nor custom[ROOT_SPAN_ID]
    (theoretically unreachable), the counters bucket must still be popped
    before the KeyError surfaces through the hook error policy."""
    root_id = "orphan-root"
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    _seed_turn_spans(session, root_id, [_span("tool", root_id, SpanName.EXECUTE_TOOL)])
    hook = RootSpanHook(session=session, store=store, score_injector=injector)
    ctx = _make_context(_TRACE_ID, root_id)
    assert ctx.runtime is not None
    ctx.runtime.state.custom.pop(TurnCustomKey.ROOT_SPAN_ID)

    with pytest.raises(KeyError):
        await hook.finally_graph(ctx, result=AgentResult(content="done"))

    store.save_span.assert_not_awaited()
    injector.inject_scores.assert_not_awaited()
    assert _TRACE_ID not in session._metric_counters


# ── same-process approval suspend→resume: whole-turn metrics ──────────────


async def _save_chat(
    hook: RootSpanHook,
    ctx: AgentContext,
    *,
    span_id: str,
    root_id: str,
    input_tokens: int,
) -> None:
    await hook._save_span(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=root_id,
        name=SpanName.CHAT.value,
        kind=SpanKind.CLIENT.value,
        start_time=1.0,
        end_time=2.0,
        attributes={
            GenAiAttr.USAGE_INPUT_TOKENS.value: input_tokens,
            GenAiAttr.USAGE_OUTPUT_TOKENS.value: 10,
        },
        status=SpanStatus(),
        ctx=ctx,
    )


async def test_suspend_resume_real_sequence_accumulates_whole_turn_metrics() -> None:
    """Drive the real same-process sequence: start_node_turn → pre-suspend
    spans → interrupt finalize (``result=None`` — the exact payload shape
    the agent's finally dispatches on GraphInterrupt; the runner never
    passes ``error``) → snapshot-restored turn state (fresh state object,
    custom TRACE_ID/ROOT_SPAN_ID restored, START_NODE_TURN not re-fired) →
    resumed spans → terminal finalize. The terminal metrics must cover
    BOTH segments, and nothing may be emitted/stashed/cleared at suspend."""
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    ctx1 = _make_context(_TRACE_ID, "ignored-start-id")
    await hook.start_node_turn(ctx1)
    assert ctx1.runtime is not None
    root_id = str(ctx1.runtime.state.custom[TurnCustomKey.ROOT_SPAN_ID])
    seeded_start = session.root_span_info[_TRACE_ID][1]

    await _save_chat(hook, ctx1, span_id="chat-pre", root_id=root_id, input_tokens=100)

    await hook.finally_graph(ctx1, result=None)

    assert store.save_span.await_count == 1  # only the pre-suspend chat span
    assert TurnCustomKey.TRAJECTORY_METRICS not in ctx1.runtime.state.custom
    assert _TRACE_ID in session._metric_counters  # bucket survives the suspend
    assert session.root_span_info[_TRACE_ID][0] == root_id

    ctx2 = _make_context(_TRACE_ID, root_id)
    await _save_chat(hook, ctx2, span_id="chat-post", root_id=root_id, input_tokens=250)

    await hook.finally_graph(ctx2, result=AgentResult(content="done"))
    await _drain_pending(hook)

    saved = [c.args[0] for c in store.save_span.call_args_list]
    assert len(saved) == 3  # chat-pre + chat-post + the single terminal root span
    root_span = saved[-1]
    assert root_span.span_id == root_id
    assert root_span.name == SpanName.INVOKE_AGENT.value
    assert root_span.start_time == seeded_start  # anchored at the original turn start
    assert ctx2.runtime is not None
    stashed = ctx2.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]
    assert stashed.total_input_tokens == 350  # 100 pre-suspend + 250 resumed
    assert stashed.llm_call_count == 2
    injector.inject_scores.assert_awaited_once()
    await_args = injector.inject_scores.await_args
    assert await_args.args == (_TRACE_ID, stashed)
    _assert_default_cost_kwargs(await_args.kwargs, root_id)
    assert _TRACE_ID not in session._metric_counters


async def test_suspended_then_terminal_error_finalize_clears_bucket() -> None:
    """Abandon path: a suspended turn that later terminal-finalizes with an
    error result (auto-deny → denied tools → error outcome) still writes the
    stash (covering the pre-suspend segment) and clears the bucket."""
    store = AsyncMock(spec=OtelSpanTraceStore)
    injector = AsyncMock(spec=L2ScoreInjector)
    session = TraceSessionState()
    hook = RootSpanHook(session=session, store=store, score_injector=injector)

    ctx1 = _make_context(_TRACE_ID, "ignored-start-id")
    await hook.start_node_turn(ctx1)
    assert ctx1.runtime is not None
    root_id = str(ctx1.runtime.state.custom[TurnCustomKey.ROOT_SPAN_ID])

    await _save_chat(hook, ctx1, span_id="chat-pre", root_id=root_id, input_tokens=70)
    await hook.finally_graph(ctx1, result=None)  # suspend — bucket stays

    ctx2 = _make_context(_TRACE_ID, root_id)
    await hook.finally_graph(
        ctx2,
        result=AgentResult(content="", stop_reason=StopReason.ERROR),
    )

    injector.inject_scores.assert_not_awaited()
    assert ctx2.runtime is not None
    stashed = ctx2.runtime.state.custom[TurnCustomKey.TRAJECTORY_METRICS]
    assert isinstance(stashed, TrajectoryMetrics)
    assert stashed.llm_call_count == 1  # the pre-suspend chat survives the abandon
    assert stashed.total_input_tokens == 70
    assert _TRACE_ID not in session._metric_counters
