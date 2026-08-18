"""Tests for TraceSessionState shared state."""

from __future__ import annotations

from modex_agent.trace.scoring import compute_metrics
from modex_agent.trace.semconv import SpanName
from modex_agent.trace.session_state import TraceSessionState
from modex_agent.trace.store import SpanModel


def test_trace_session_state_initialization() -> None:
    state = TraceSessionState()
    assert state.root_span_info == {}
    assert state.llm_start_times == {}
    assert state.llm_request_attrs == {}
    assert state.iteration_start_times == {}
    assert state.tool_batch_info == {}
    assert state.turn_usage == {}
    assert state.user_inputs == {}


def test_root_span_info_returns_dict() -> None:
    state = TraceSessionState()
    assert isinstance(state.root_span_info, dict)
    state.root_span_info["t1"] = ("span-1", 1000.0)
    assert state.root_span_info["t1"] == ("span-1", 1000.0)


def test_clear_trace_removes_all_state() -> None:
    state = TraceSessionState()
    state.root_span_info["t1"] = ("span-1", 1000.0)
    state.llm_start_times["t1"] = 1.0
    state.llm_request_attrs["t1"] = {"model": "test"}
    state.iteration_start_times["t1"] = 2.0
    state.tool_batch_info["t1"] = (3.0, "span-1", [])
    state.turn_usage["t1"] = {"prompt_tokens": 10, "completion_tokens": 20}
    state.user_inputs["t1"] = "hello"
    state.accumulate_span(
        "t1",
        "span-1",
        SpanModel(
            trace_id="t1",
            span_id="chat-1",
            parent_span_id="span-1",
            name=SpanName.CHAT.value,
            start_time=1.0,
            end_time=2.0,
        ),
    )
    assert state.read_metrics("t1", "span-1").llm_call_count == 1

    state.clear_trace("t1")

    assert "t1" not in state.root_span_info
    assert "t1" not in state.llm_start_times
    assert "t1" not in state.llm_request_attrs
    assert "t1" not in state.iteration_start_times
    assert "t1" not in state.tool_batch_info
    assert "t1" not in state.turn_usage
    assert "t1" not in state.user_inputs
    assert state.read_metrics("t1", "span-1") == compute_metrics([])


def test_clear_trace_nonexistent_no_error() -> None:
    state = TraceSessionState()
    state.clear_trace("nonexistent")


def test_multiple_traces_isolated() -> None:
    state = TraceSessionState()
    state.root_span_info["t1"] = ("span-1", 100.0)
    state.root_span_info["t2"] = ("span-2", 200.0)
    state.llm_start_times["t1"] = 1.0
    state.llm_start_times["t2"] = 2.0
    state.turn_usage["t1"] = {"prompt_tokens": 5}
    state.turn_usage["t2"] = {"prompt_tokens": 50}

    state.clear_trace("t1")

    assert "t1" not in state.root_span_info
    assert "t2" in state.root_span_info
    assert state.root_span_info["t2"] == ("span-2", 200.0)
    assert "t1" not in state.llm_start_times
    assert state.llm_start_times["t2"] == 2.0
    assert "t1" not in state.turn_usage
    assert state.turn_usage["t2"] == {"prompt_tokens": 50}
