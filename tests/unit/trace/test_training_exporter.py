"""Unit tests for TrainingDataExporter — SFT and DPO dataset derivation.

Tests cover:
- SFT export produces valid OpenAI messages JSONL
- ``tool_calls.function.arguments`` is a JSON string
- ``reasoning_content`` wrapped in ``<think>...</think>``
- DPO pairs from approval data
- Deduplication (exact hash tier)
- L2 scoring computes correct values
- Scope-aware filtering warns on cross-tenant
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from modex_agent.trace.scoring import (
    TrajectoryMetrics,
    compute_metrics,
    extract_final_response,
)
from modex_agent.trace.semconv import GenAiAttr, SpanStatusCode
from modex_agent.trace.store import SpanModel, SpanStatus, TraceQuery
from modex_agent.trace.training_exporter import (
    DPOPair,
    ExportResult,
    SFTExample,
    TrainingDataExporter,
    _edit_distance_ratio,
    _extract_system_message,
    _extract_user_message,
    _is_training_relevant,
    _jaccard_similarity,
    _levenshtein,
    _ngram_set,
    _tenant_of,
    _wrap_reasoning,
)

# ── In-memory trace store for tests ────────────────────────────────────


class InMemoryTraceStore(TraceQuery):
    """Simple in-memory TraceQuery for testing."""

    def __init__(self) -> None:
        self._spans: list[SpanModel] = []

    async def list_by_session(self, session_id: str) -> list[SpanModel]:
        return [
            s
            for s in self._spans
            if s.attributes.get(GenAiAttr.CONVERSATION_ID.value) == session_id
        ]

    async def list_by_trace_id(self, trace_id: str) -> list[SpanModel]:
        return [s for s in self._spans if s.trace_id == trace_id]


# ── Span builder ───────────────────────────────────────────────────────


def _make_span(
    trace_id: str = "trace-001",
    session_id: str = "conv-1:agent-1",
    agent_name: str = "react_main",
    name: str = "chat",
    *,
    start_time: float = 1000.0,
    attributes: dict[str, Any] | None = None,
    status_code: SpanStatusCode = SpanStatusCode.OK,
) -> SpanModel:
    """Build a minimal SpanModel with common attributes pre-filled."""
    attrs: dict[str, Any] = {
        GenAiAttr.AGENT_NAME.value: agent_name,
        GenAiAttr.CONVERSATION_ID.value: session_id,
    }
    if attributes:
        attrs.update(attributes)
    return SpanModel(
        trace_id=trace_id,
        span_id=f"span-{trace_id}-{start_time}",
        parent_span_id=None,
        name=name,
        kind="CLIENT" if name == "chat" else "INTERNAL",
        start_time=start_time,
        end_time=start_time + 0.1,
        attributes=attrs,
        status=SpanStatus(code=status_code),
    )


def _make_trajectory(
    trace_id: str = "trace-001",
    session_id: str = "conv-1:agent-1",
    agent_name: str = "react_main",
    *,
    with_tool_calls: bool = True,
    with_reasoning: bool = True,
    training_relevant: bool = True,
    approval: bool | None = None,
    user_message: str = "What is 2+2?",
    final_content: str = "The answer is 4.",
    reasoning: str = "2 plus 2 equals 4",
    tool_success: bool = True,
    usage: dict[str, int] | None = None,
    start_ts: float = 1000.0,
) -> list[SpanModel]:
    """Build a realistic trajectory of SpanModels."""
    spans: list[SpanModel] = []
    ts = start_ts

    # invoke_agent span (TURN_START)
    spans.append(
        _make_span(
            trace_id=trace_id,
            session_id=session_id,
            agent_name=agent_name,
            name="invoke_agent",
            start_time=ts,
            attributes={
                "recent_messages": [{"role": "user", "content": user_message}],
            },
        )
    )
    ts += 1

    # chat span with tool_calls
    if with_tool_calls:
        spans.append(
            _make_span(
                trace_id=trace_id,
                session_id=session_id,
                agent_name=agent_name,
                name="chat",
                start_time=ts,
                attributes={
                    GenAiAttr.OUTPUT_MESSAGES.value: [
                        {"role": "assistant", "parts": [{"type": "text", "content": ""}]}
                    ],
                    "has_tool_calls": True,
                    GenAiAttr.RESPONSE_FINISH_REASONS.value: ["tool_calls"],
                    GenAiAttr.OUTPUT_TOOL_CALLS.value: [
                        {"tool_name": "calculator", "arguments": '{"expr": "2+2"}'}
                    ],
                    GenAiAttr.USAGE_INPUT_TOKENS.value: 10,
                    GenAiAttr.USAGE_OUTPUT_TOKENS.value: 5,
                },
            )
        )
        ts += 1

        # execute_tool span
        spans.append(
            _make_span(
                trace_id=trace_id,
                session_id=session_id,
                agent_name=agent_name,
                name="execute_tool",
                start_time=ts,
                attributes={
                    GenAiAttr.TOOL_NAME.value: "calculator",
                    GenAiAttr.TOOL_RESULT.value: "4",
                },
                status_code=SpanStatusCode.OK if tool_success else SpanStatusCode.ERROR,
            )
        )
        ts += 1

    # Final chat span (no tool_calls)
    final_attrs: dict[str, Any] = {
        GenAiAttr.OUTPUT_MESSAGES.value: [
            {"role": "assistant", "parts": [{"type": "text", "content": final_content}]}
        ],
        "has_tool_calls": False,
        GenAiAttr.RESPONSE_FINISH_REASONS.value: ["stop"],
        GenAiAttr.USAGE_INPUT_TOKENS.value: 20,
        GenAiAttr.USAGE_OUTPUT_TOKENS.value: 10,
    }
    if with_reasoning:
        final_attrs[GenAiAttr.OUTPUT_REASONING_CONTENT.value] = reasoning
        final_attrs[GenAiAttr.USAGE_REASONING_TOKENS.value] = 15
    if usage:
        final_attrs[GenAiAttr.USAGE_INPUT_TOKENS.value] = usage.get("input_tokens", 20)
        final_attrs[GenAiAttr.USAGE_OUTPUT_TOKENS.value] = usage.get("output_tokens", 10)
        if "reasoning_tokens" in usage:
            final_attrs[GenAiAttr.USAGE_REASONING_TOKENS.value] = usage["reasoning_tokens"]
    spans.append(
        _make_span(
            trace_id=trace_id,
            session_id=session_id,
            agent_name=agent_name,
            name="chat",
            start_time=ts,
            attributes=final_attrs,
        )
    )
    ts += 1

    # human.review span (optional approval)
    if approval is not None:
        spans.append(
            _make_span(
                trace_id=trace_id,
                session_id=session_id,
                agent_name=agent_name,
                name="human.review",
                start_time=ts,
                attributes={"approved": approval},
            )
        )
        ts += 1

    # training_tag span
    spans.append(
        _make_span(
            trace_id=trace_id,
            session_id=session_id,
            agent_name=agent_name,
            name="training_tag",
            start_time=ts,
            attributes={"gen_ai.training.relevant": training_relevant},
        )
    )

    return spans


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file and return a list of parsed dicts."""
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


# ── Tests: SFT export ──────────────────────────────────────────────────


class TestSFTExport:
    async def test_sft_produces_valid_openai_messages_jsonl(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory():
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])

        assert isinstance(result, ExportResult)
        assert result.sft_count == 1
        assert result.deduped_count == 0
        assert result.output_path.exists()

        examples = _load_jsonl(result.output_path)
        assert len(examples) == 1
        example = examples[0]

        # Must have a "messages" key with a list.
        assert "messages" in example
        messages = example["messages"]
        assert isinstance(messages, list)
        assert len(messages) >= 2

        # Every message has a "role".
        roles = [m["role"] for m in messages]
        assert "user" in roles
        assert "assistant" in roles

        # Score metadata is included.
        assert "score" in example
        assert "tool_success_rate" in example["score"]

    async def test_tool_calls_arguments_is_json_string(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(with_tool_calls=True):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])

        examples = _load_jsonl(result.output_path)
        messages = examples[0]["messages"]

        # Find the assistant message with tool_calls.
        assistant_with_tools = [
            m for m in messages if m["role"] == "assistant" and m.get("tool_calls")
        ]
        assert len(assistant_with_tools) == 1

        tool_calls = assistant_with_tools[0]["tool_calls"]
        assert len(tool_calls) == 1
        tc = tool_calls[0]
        assert tc["id"].startswith("call_")
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "calculator"
        # arguments MUST be a JSON string, not a dict.
        args = tc["function"]["arguments"]
        assert isinstance(args, str)
        parsed = json.loads(args)
        assert parsed == {"expr": "2+2"}

    async def test_tool_result_message_has_tool_call_id(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(with_tool_calls=True):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])

        examples = _load_jsonl(result.output_path)
        messages = examples[0]["messages"]

        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"].startswith("call_")
        assert tool_msgs[0]["content"] == "4"

    async def test_reasoning_wrapped_in_think_tags(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(
            with_reasoning=True,
            reasoning="I need to calculate 2+2",
            final_content="The answer is 4.",
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])

        examples = _load_jsonl(result.output_path)
        messages = examples[0]["messages"]

        # The last assistant message should have <think>...</think> wrapping.
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        final_assistant = assistant_msgs[-1]
        content = final_assistant["content"]
        assert content.startswith("<think>")
        assert "</think>" in content
        assert "I need to calculate 2+2" in content
        assert "The answer is 4." in content

        # reasoning_content field should also be present.
        assert final_assistant.get("reasoning_content") == "I need to calculate 2+2"

    async def test_no_reasoning_no_think_tags(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(with_reasoning=False):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])

        examples = _load_jsonl(result.output_path)
        messages = examples[0]["messages"]
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        final_assistant = assistant_msgs[-1]
        assert "<think>" not in final_assistant["content"]
        assert final_assistant["content"] == "The answer is 4."

    async def test_non_training_relevant_skipped(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(training_relevant=False):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])

        assert result.sft_count == 0

    def test_any_true_tag_accepts_trajectory(self) -> None:
        """Locks the exporter's any-true acceptance semantics — the reason a
        single stray suspend-time ``relevant=true`` tag is fatal: a
        [true, false] trajectory (the legacy suspend-then-failed shape) is
        exported. Exporter code is intentionally unchanged; the fix lives in
        TrainingDataHook, which no longer emits the true tag at suspend."""
        spans = _make_trajectory(training_relevant=False)
        spans.append(
            _make_span(
                name="training_tag",
                start_time=5000.0,
                attributes={"gen_ai.training.relevant": True},
            )
        )

        assert _is_training_relevant(spans) is True

    async def test_suspend_then_failed_turn_not_exported(self, tmp_path: Path) -> None:
        """A suspended-then-failed turn now carries exactly one false tag
        (zero tags at suspend, one false at the terminal finalize — the
        TrainingDataHook suspend sentinel). The any-true exporter therefore
        rejects it. The same trajectory with a legacy suspend-time true tag
        appended WOULD be exported, which is the defect this locks out."""
        store = InMemoryTraceStore()
        for span in _make_trajectory(training_relevant=False):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])
        assert result.sft_count == 0

        legacy = InMemoryTraceStore()
        for span in _make_trajectory(training_relevant=False):
            legacy._spans.append(span)
        legacy._spans.append(
            _make_span(
                name="training_tag",
                start_time=5000.0,
                attributes={"gen_ai.training.relevant": True},
            )
        )
        legacy_exporter = TrainingDataExporter(legacy, output_dir=tmp_path)
        legacy_result = await legacy_exporter.export_sft(session_ids=["conv-1:agent-1"])
        assert legacy_result.sft_count == 1  # proves the delta is the stray true tag

    async def test_empty_session_ids_returns_empty(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=None)
        assert result.sft_count == 0

        result = await exporter.export_sft(session_ids=[])
        assert result.sft_count == 0

    async def test_time_range_filter(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        # Trajectory at t=1000
        for span in _make_trajectory(trace_id="trace-A", start_ts=1000.0):
            store._spans.append(span)
        # Trajectory at t=2000
        for span in _make_trajectory(trace_id="trace-B", start_ts=2000.0):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)

        # Only the first trajectory is before t=1500.
        result = await exporter.export_sft(
            session_ids=["conv-1:agent-1"],
            since=900,
            until=1500,
        )
        assert result.sft_count == 1


# ── Tests: DPO export ──────────────────────────────────────────────────


class TestDPOExport:
    async def test_dpo_pairs_from_approval_data(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()

        # Approved trajectory with high score (1.0 tool success, high reasoning)
        for span in _make_trajectory(
            trace_id="trace-approved",
            approval=True,
            final_content="The answer is 4.",
            tool_success=True,
            reasoning="I need to carefully calculate the sum of 2 and 2",
            usage={"input_tokens": 100, "output_tokens": 50, "reasoning_tokens": 500},
        ):
            store._spans.append(span)

        # Denied trajectory with low score (0.0 tool success, failed tool)
        for span in _make_trajectory(
            trace_id="trace-denied",
            approval=False,
            final_content="I think it might be 5.",
            tool_success=False,
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_dpo(session_ids=["conv-1:agent-1"])

        assert result.dpo_count == 1
        pairs = _load_jsonl(result.output_path)
        assert len(pairs) == 1
        pair = pairs[0]

        assert pair["prompt"] == "What is 2+2?"
        assert pair["chosen"] == "The answer is 4."
        assert pair["rejected"] == "I think it might be 5."
        assert pair["chosen_model"] == "react_main"
        assert pair["rejected_model"] == "react_main"
        assert pair["chosen_tool_success_rate"] >= pair["rejected_tool_success_rate"]

    async def test_dpo_no_approval_data_skipped(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(approval=None):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_dpo(session_ids=["conv-1:agent-1"])
        assert result.dpo_count == 0

    async def test_dpo_score_gap_filter(self, tmp_path: Path) -> None:
        """Both trajectories have identical scores → gap < 0.5 → filtered out."""
        store = InMemoryTraceStore()
        for span in _make_trajectory(
            trace_id="trace-A",
            approval=True,
            final_content="Answer A.",
            tool_success=True,
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-B",
            approval=False,
            final_content="Answer B.",
            tool_success=True,
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_dpo(session_ids=["conv-1:agent-1"])

        # Both have identical scores (1.0 tool success) → gap = 0 < 0.5 → no pairs.
        assert result.dpo_count == 0

    async def test_dpo_gap_filter_is_directional(self, tmp_path: Path) -> None:
        """Gap filter is directional: chosen - rejected, not abs(chosen - rejected).

        Approved trajectory has LOWER tool success than denied → negative gap → filtered.
        """
        store = InMemoryTraceStore()
        for span in _make_trajectory(
            trace_id="trace-approved-low",
            approval=True,
            final_content="The answer is 4.",
            tool_success=False,
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-denied-high",
            approval=False,
            final_content="I think it might be 42.",
            tool_success=True,
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_dpo(session_ids=["conv-1:agent-1"])

        assert result.dpo_count == 0

    async def test_dpo_refusal_filter(self, tmp_path: Path) -> None:
        """A refusal response is filtered out."""
        store = InMemoryTraceStore()
        for span in _make_trajectory(
            trace_id="trace-approved",
            approval=True,
            final_content="The answer is 4.",
            tool_success=True,
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-denied",
            approval=False,
            final_content="I'm sorry, I can't help with that.",
            tool_success=False,
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_dpo(session_ids=["conv-1:agent-1"])
        # The rejected response is a refusal → filtered.
        assert result.dpo_count == 0


# ── Tests: Deduplication ───────────────────────────────────────────────


class TestDeduplication:
    async def test_exact_hash_dedup(self, tmp_path: Path) -> None:
        """Two identical trajectories → one deduped."""
        store = InMemoryTraceStore()
        for span in _make_trajectory(trace_id="trace-A"):
            store._spans.append(span)
        for span in _make_trajectory(trace_id="trace-B"):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])

        assert result.sft_count == 1
        assert result.deduped_count == 1

    async def test_different_trajectories_not_deduped(self, tmp_path: Path) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(
            trace_id="trace-A",
            user_message="What is the capital of France?",
            final_content="The capital of France is Paris.",
            reasoning="France is a country in Europe, its capital is Paris",
            tool_success=True,
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-B",
            user_message="How do you make a pancake?",
            final_content="Mix flour, eggs, and milk, then cook on a pan.",
            reasoning="To make pancakes you need flour eggs and milk",
            tool_success=True,
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_sft(session_ids=["conv-1:agent-1"])

        assert result.sft_count == 2
        assert result.deduped_count == 0

    async def test_dpo_exact_hash_dedup(self, tmp_path: Path) -> None:
        """Identical DPO pairs are deduped."""
        store = InMemoryTraceStore()
        # Two identical approved/denied pairs (different trace_ids but same content)
        for span in _make_trajectory(
            trace_id="trace-A1",
            approval=True,
            final_content="4",
            tool_success=True,
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-B1",
            approval=False,
            final_content="5",
            tool_success=False,
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-A2",
            approval=True,
            final_content="4",
            tool_success=True,
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-B2",
            approval=False,
            final_content="5",
            tool_success=False,
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        result = await exporter.export_dpo(session_ids=["conv-1:agent-1"])

        # 4 identical pairs (A1×B1, A1×B2, A2×B1, A2×B2) → 1 kept, 3 deduped.
        assert result.dpo_count == 1
        assert result.deduped_count == 3


# ── Tests: L2 scoring ──────────────────────────────────────────────────


class TestL2Scoring:
    # ── Basic sanity tests ─────────────────────────────────────────────

    def test_compute_metrics_returns_metrics_type(self) -> None:
        spans = _make_trajectory(tool_success=True, with_reasoning=True)
        metrics = compute_metrics(spans)
        assert isinstance(metrics, TrajectoryMetrics)

    def test_compute_metrics_all_success(self) -> None:
        spans = _make_trajectory(tool_success=True, with_reasoning=True)
        metrics = compute_metrics(spans)
        assert metrics.tool_success_rate == 1.0
        assert metrics.tool_call_count == 1
        assert metrics.error_tool_count == 0
        assert metrics.llm_call_count == 2
        assert metrics.total_reasoning_tokens == 15
        assert metrics.has_reasoning is True

    def test_compute_metrics_failed_tool(self) -> None:
        spans = _make_trajectory(tool_success=False, with_reasoning=True)
        metrics = compute_metrics(spans)
        assert metrics.tool_success_rate == 0.0
        assert metrics.error_tool_count == 1

    def test_compute_metrics_no_tools(self) -> None:
        spans = _make_trajectory(with_tool_calls=False, with_reasoning=True)
        metrics = compute_metrics(spans)
        # No tools → tool_success_rate defaults to 1.0
        assert metrics.tool_success_rate == 1.0
        assert metrics.tool_call_count == 0

    def test_compute_metrics_no_reasoning(self) -> None:
        spans = _make_trajectory(with_reasoning=False)
        metrics = compute_metrics(spans)
        assert metrics.total_reasoning_tokens == 0
        assert metrics.has_reasoning is False

    # ── Direct formula tests (one per metric field) ────────────────────

    def test_tool_success_rate_mixed_success_and_error(self) -> None:
        """tool_success_rate = (total - error) / total with mixed tool spans."""
        spans = [
            _make_span(name="execute_tool", start_time=1000.0),
            _make_span(name="execute_tool", start_time=1001.0, status_code=SpanStatusCode.ERROR),
        ]
        metrics = compute_metrics(spans)
        assert metrics.tool_success_rate == pytest.approx(0.5)

    def test_tool_call_count_equals_execute_tool_span_count(self) -> None:
        spans = [
            _make_span(name="execute_tool", start_time=1000.0),
            _make_span(name="execute_tool", start_time=1001.0),
            _make_span(name="execute_tool", start_time=1002.0),
        ]
        metrics = compute_metrics(spans)
        assert metrics.tool_call_count == 3

    def test_error_tool_count_equals_error_status_count(self) -> None:
        spans = [
            _make_span(name="execute_tool", start_time=1000.0, status_code=SpanStatusCode.ERROR),
            _make_span(name="execute_tool", start_time=1001.0, status_code=SpanStatusCode.ERROR),
            _make_span(name="execute_tool", start_time=1002.0),
        ]
        metrics = compute_metrics(spans)
        assert metrics.error_tool_count == 2

    def test_iteration_count_equals_iteration_start_count(self) -> None:
        spans = [
            _make_span(name="iteration.start", start_time=1000.0),
            _make_span(name="iteration.start", start_time=1001.0),
            _make_span(name="iteration.start", start_time=1002.0),
        ]
        metrics = compute_metrics(spans)
        assert metrics.iteration_count == 3

    def test_iteration_count_zero_when_no_iteration_spans(self) -> None:
        spans = [_make_span(name="chat", start_time=1000.0)]
        metrics = compute_metrics(spans)
        assert metrics.iteration_count == 0

    def test_llm_call_count_equals_chat_span_count(self) -> None:
        spans = [
            _make_span(name="chat", start_time=1000.0),
            _make_span(name="chat", start_time=1001.0),
        ]
        metrics = compute_metrics(spans)
        assert metrics.llm_call_count == 2

    def test_total_input_tokens_sums_chat_spans_not_root(self) -> None:
        """Tokens are summed from chat spans only, NOT from invoke_agent root."""
        spans = [
            _make_span(
                name="invoke_agent",
                start_time=999.0,
                attributes={GenAiAttr.USAGE_INPUT_TOKENS.value: 100},
            ),
            _make_span(
                name="chat",
                start_time=1000.0,
                attributes={GenAiAttr.USAGE_INPUT_TOKENS.value: 10},
            ),
            _make_span(
                name="chat",
                start_time=1001.0,
                attributes={GenAiAttr.USAGE_INPUT_TOKENS.value: 20},
            ),
        ]
        metrics = compute_metrics(spans)
        assert metrics.total_input_tokens == 30  # 10 + 20, NOT 130

    def test_total_output_tokens_sums_chat_spans(self) -> None:
        spans = [
            _make_span(
                name="chat",
                start_time=1000.0,
                attributes={GenAiAttr.USAGE_OUTPUT_TOKENS.value: 5},
            ),
            _make_span(
                name="chat",
                start_time=1001.0,
                attributes={GenAiAttr.USAGE_OUTPUT_TOKENS.value: 15},
            ),
        ]
        metrics = compute_metrics(spans)
        assert metrics.total_output_tokens == 20

    def test_total_reasoning_tokens_sums_chat_spans(self) -> None:
        spans = [
            _make_span(
                name="chat",
                start_time=1000.0,
                attributes={GenAiAttr.USAGE_REASONING_TOKENS.value: 10},
            ),
            _make_span(
                name="chat",
                start_time=1001.0,
                attributes={GenAiAttr.USAGE_REASONING_TOKENS.value: 20},
            ),
        ]
        metrics = compute_metrics(spans)
        assert metrics.total_reasoning_tokens == 30

    def test_total_reasoning_tokens_zero_for_non_reasoning(self) -> None:
        spans = [
            _make_span(
                name="chat",
                start_time=1000.0,
                attributes={GenAiAttr.USAGE_INPUT_TOKENS.value: 10},
            ),
        ]
        metrics = compute_metrics(spans)
        assert metrics.total_reasoning_tokens == 0

    def test_api_latency_avg_s_averages_chat_durations(self) -> None:
        chat1 = _make_span(name="chat", start_time=1000.0).model_copy(update={"end_time": 1002.0})
        chat2 = _make_span(name="chat", start_time=1003.0).model_copy(update={"end_time": 1007.0})
        metrics = compute_metrics([chat1, chat2])
        # durations: 2.0 and 4.0 → avg 3.0
        assert metrics.api_latency_avg_s == pytest.approx(3.0)

    def test_cache_hit_rate_is_cache_read_over_input(self) -> None:
        """cache_hit_rate = cache_read / input, NOT cache_read / (cache_read + input)."""
        spans = [
            _make_span(
                name="chat",
                start_time=1000.0,
                attributes={
                    GenAiAttr.USAGE_INPUT_TOKENS.value: 100,
                    GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value: 30,
                },
            ),
        ]
        metrics = compute_metrics(spans)
        assert metrics.cache_hit_rate == pytest.approx(0.3)  # 30/100, NOT 30/130

    def test_response_token_ratio_is_output_over_total(self) -> None:
        spans = [
            _make_span(
                name="chat",
                start_time=1000.0,
                attributes={
                    GenAiAttr.USAGE_INPUT_TOKENS.value: 30,
                    GenAiAttr.USAGE_OUTPUT_TOKENS.value: 70,
                },
            ),
        ]
        metrics = compute_metrics(spans)
        assert metrics.response_token_ratio == pytest.approx(0.7)  # 70/(30+70)

    def test_has_reasoning_is_bool_of_reasoning_tokens(self) -> None:
        # With reasoning tokens → True
        with_reasoning = [
            _make_span(
                name="chat",
                start_time=1000.0,
                attributes={GenAiAttr.USAGE_REASONING_TOKENS.value: 1},
            ),
        ]
        assert compute_metrics(with_reasoning).has_reasoning is True

        # Without reasoning tokens → False
        without_reasoning = [
            _make_span(
                name="chat",
                start_time=1000.0,
                attributes={GenAiAttr.USAGE_INPUT_TOKENS.value: 10},
            ),
        ]
        assert compute_metrics(without_reasoning).has_reasoning is False

    # ── extract_final_response fallback ────────────────────────────────

    def test_extract_final_response_fallback_to_root_output(self) -> None:
        """When no tool-call-free chat span exists, fall back to invoke_agent output."""
        spans = [
            _make_span(
                name="invoke_agent",
                start_time=1000.0,
                attributes={GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT.value: "root output"},
            ),
            _make_span(
                name="chat",
                start_time=1001.0,
                attributes={
                    GenAiAttr.OUTPUT_MESSAGES.value: [
                        {
                            "role": "assistant",
                            "parts": [{"type": "text", "content": "tool call response"}],
                        }
                    ],
                    GenAiAttr.OUTPUT_TOOL_CALLS.value: [{"tool_name": "calc", "arguments": "{}"}],
                },
            ),
        ]
        assert extract_final_response(spans) == "root output"

    # ── Legacy helpers removed ─────────────────────────────────────────

    def test_legacy_score_helpers_removed(self) -> None:
        """Legacy scalar combine/cap/rating helpers are gone from the scoring module."""
        from modex_agent.trace import scoring

        public = {name for name in dir(scoring) if not name.startswith("_")}
        assert "TrajectoryMetrics" in public
        assert "compute_metrics" in public
        assert not any("overall" in n for n in public)
        assert not any("rating" in n for n in public)


# ── Tests: Scope-aware filtering ───────────────────────────────────────


class TestScopeAwareFiltering:
    async def test_cross_tenant_warns(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(
            trace_id="trace-A",
            session_id="tenant-1:agent-1",
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-B",
            session_id="tenant-2:agent-1",
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        with caplog.at_level(logging.WARNING, logger="modex_agent.trace.training_exporter"):
            await exporter.export_sft(
                session_ids=["tenant-1:agent-1", "tenant-2:agent-1"],
            )

        assert any("Cross-tenant" in msg for msg in caplog.messages)

    async def test_cross_tenant_suppressed_with_opt_in(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(
            trace_id="trace-A",
            session_id="tenant-1:agent-1",
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-B",
            session_id="tenant-2:agent-1",
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        with caplog.at_level(logging.WARNING, logger="modex_agent.trace.training_exporter"):
            await exporter.export_sft(
                session_ids=["tenant-1:agent-1", "tenant-2:agent-1"],
                allow_cross_tenant=True,
            )

        assert not any("Cross-tenant" in msg for msg in caplog.messages)

    async def test_same_tenant_no_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryTraceStore()
        for span in _make_trajectory(
            trace_id="trace-A",
            session_id="conv-1:agent-1",
        ):
            store._spans.append(span)
        for span in _make_trajectory(
            trace_id="trace-B",
            session_id="conv-1:agent-2",
        ):
            store._spans.append(span)

        exporter = TrainingDataExporter(store, output_dir=tmp_path)
        with caplog.at_level(logging.WARNING, logger="modex_agent.trace.training_exporter"):
            await exporter.export_sft(
                session_ids=["conv-1:agent-1", "conv-1:agent-2"],
            )

        assert not any("Cross-tenant" in msg for msg in caplog.messages)

    def test_tenant_extraction(self) -> None:
        assert _tenant_of("conv-1:agent-1") == "conv-1"
        assert _tenant_of("conv-1:agent-1:inv-1") == "conv-1"
        assert _tenant_of("no-colon") == "no-colon"


# ── Tests: Helper functions ────────────────────────────────────────────


class TestHelpers:
    def test_wrap_reasoning_with_content(self) -> None:
        result = _wrap_reasoning("thinking", "answer")
        assert result == "<think>thinking</think>\nanswer"

    def test_wrap_reasoning_empty(self) -> None:
        result = _wrap_reasoning("", "answer")
        assert result == "answer"

    def test_wrap_reasoning_no_content(self) -> None:
        result = _wrap_reasoning("thinking", "")
        assert result == "<think>thinking</think>"

    def test_ngram_set_short_text(self) -> None:
        assert _ngram_set("ab") == frozenset({"ab"})

    def test_ngram_set_normal(self) -> None:
        result = _ngram_set("abc", n=3)
        assert result == frozenset({"abc"})

    def test_ngram_set_longer(self) -> None:
        result = _ngram_set("abcd", n=3)
        assert result == frozenset({"abc", "bcd"})

    def test_jaccard_identical(self) -> None:
        a = _ngram_set("hello world")
        assert _jaccard_similarity(a, a) == 1.0

    def test_jaccard_disjoint(self) -> None:
        a = _ngram_set("aaa")
        b = _ngram_set("zzz")
        assert _jaccard_similarity(a, b) == 0.0

    def test_jaccard_both_empty(self) -> None:
        assert _jaccard_similarity(frozenset(), frozenset()) == 1.0

    def test_levenshtein_identical(self) -> None:
        assert _levenshtein("abc", "abc") == 0

    def test_levenshtein_one_edit(self) -> None:
        assert _levenshtein("abc", "abd") == 1

    def test_levenshtein_empty(self) -> None:
        assert _levenshtein("", "abc") == 3

    def test_edit_distance_ratio_identical(self) -> None:
        assert _edit_distance_ratio("abc", "abc") == 0.0

    def test_edit_distance_ratio_completely_different(self) -> None:
        ratio = _edit_distance_ratio("aaaa", "zzzz")
        assert ratio == 1.0

    def test_edit_distance_ratio_both_empty(self) -> None:
        assert _edit_distance_ratio("", "") == 0.0


# ── Tests: Data models ─────────────────────────────────────────────────


class TestDataModels:
    def test_sft_example_frozen(self) -> None:
        example = SFTExample(messages=[{"role": "user", "content": "hi"}])
        with pytest.raises(ValidationError):
            example.messages = []  # type: ignore[misc]

    def test_sft_example_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            SFTExample(messages=[], unknown_field="bad")  # type: ignore[call-arg]

    def test_dpo_pair_frozen(self) -> None:
        pair = DPOPair(
            prompt="q",
            chosen="a",
            chosen_model="m",
            chosen_tool_success_rate=1.0,
            rejected="b",
            rejected_model="m",
            rejected_tool_success_rate=0.0,
        )
        with pytest.raises(ValidationError):
            pair.chosen = "c"  # type: ignore[misc]

    def test_trajectory_metrics_frozen(self) -> None:
        metrics = TrajectoryMetrics(
            tool_success_rate=1.0,
            tool_call_count=1,
            error_tool_count=0,
            iteration_count=0,
            llm_call_count=1,
            total_input_tokens=10,
            total_output_tokens=5,
            total_reasoning_tokens=0,
            api_latency_avg_s=0.1,
            cache_hit_rate=0.0,
            response_token_ratio=0.33,
            has_reasoning=False,
        )
        with pytest.raises(ValidationError):
            metrics.tool_success_rate = 0.5  # type: ignore[misc]

    def test_export_result_defaults(self) -> None:
        result = ExportResult(output_path=Path("/tmp/out.jsonl"))
        assert result.sft_count == 0
        assert result.dpo_count == 0
        assert result.deduped_count == 0

    def test_span_record_frozen(self) -> None:
        span = SpanModel(
            trace_id="t1",
            span_id="s1",
            name="chat",
            start_time=1000.0,
        )
        with pytest.raises(ValidationError):
            span.trace_id = "t2"  # type: ignore[misc]


# ── Tests: _extract_system_message / _extract_user_message ────────────


class TestExtractors:
    def test_training_exporter_reads_system_prompt(self) -> None:
        spans = [
            _make_span(
                name="agent.start",
                start_time=999.0,
                attributes={
                    GenAiAttr.SYSTEM_INSTRUCTIONS.value: "You are a helpful assistant.",
                },
            ),
            _make_span(
                name="invoke_agent",
                start_time=1000.0,
            ),
        ]
        result = _extract_system_message(spans)
        assert result == "You are a helpful assistant."

    def test_training_exporter_reads_system_prompt_fallback_root(self) -> None:
        spans = [
            _make_span(
                name="invoke_agent",
                start_time=1000.0,
                attributes={
                    GenAiAttr.SYSTEM_INSTRUCTIONS.value: "Fallback prompt.",
                },
            ),
        ]
        result = _extract_system_message(spans)
        assert result == "Fallback prompt."

    def test_training_exporter_reads_user_message(self) -> None:
        spans = [
            _make_span(
                name="invoke_agent",
                start_time=1000.0,
                attributes={
                    GenAiAttr.LANGFUSE_OBSERVATION_INPUT.value: "Hello, agent!",
                },
            ),
        ]
        result = _extract_user_message(spans)
        assert result == "Hello, agent!"

    def test_training_exporter_returns_empty_when_no_agent_start(self) -> None:
        spans = [
            _make_span(
                name="chat",
                start_time=1001.0,
            ),
        ]
        assert _extract_system_message(spans) is None
        assert _extract_user_message(spans) == ""
