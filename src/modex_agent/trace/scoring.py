"""Agent trajectory metrics — L2 observability for eval and training data derivation.

Extracted from training_exporter.py to share between:
- TrainingDataExporter (SFT/DPO export)
- L2ScoreInjector (Langfuse score injection, Layer 1 eval)
"""

from __future__ import annotations

from collections import deque
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.trace.pricing import PerModelUsage
from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel

# ── Data models ────────────────────────────────────────────────────────


class TrajectoryMetrics(BaseModel):
    """Direction-clear observability metrics for one agent trajectory.

    Every field has an unambiguous "good" direction (high, low, or neutral)
    so downstream eval and training code can filter/rank without ad-hoc
    weight combinations.

    Fields:
        tool_success_rate: non-error execute_tool / total execute_tool
            (0-1; 1.0 when no tool spans). high=good.
        tool_call_count: count of execute_tool spans. neutral reference.
        error_tool_count: execute_tool spans with ERROR status. low=good.
        iteration_count: count of iteration.start spans. In STANDARD/MINIMAL
            tier (no iteration spans) this is 0. low=good.
        llm_call_count: count of chat spans. neutral reference.
        total_input_tokens: sum of gen_ai.usage.input_tokens from chat spans
            only (NOT from invoke_agent root span — cumulative usage would
            double-count). high=cost.
        total_output_tokens: sum of gen_ai.usage.output_tokens from chat
            spans only. high=cost.
        total_reasoning_tokens: sum of gen_ai.usage.reasoning.output_tokens
            from chat spans. 0 for non-reasoning models (GPT-4o, Claude,
            DeepSeek-V3). neutral (cost reference, not quality).
        api_latency_avg_s: average wall-clock duration of chat spans
            (end_time - start_time). 0.0 when no chat spans. low=good.
        cache_hit_rate: cache_read_input_tokens / (input_tokens +
            cache_read_input_tokens) — TokenUsage.input_tokens is the UNCACHED
            count, so the prompt total is uncached + cached. 0.0 when total
            input is 0. high=good.
        response_token_ratio: output / (input + output) tokens.
            0.0 when total is 0. neutral.
        has_reasoning: total_reasoning_tokens > 0. neutral (model
            capability indicator).
        per_model_usage: additive four-bucket usage grouped by chat-span
            response model for local turn-cost reduction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_success_rate: float
    tool_call_count: int
    error_tool_count: int
    iteration_count: int
    llm_call_count: int
    total_input_tokens: int
    total_output_tokens: int
    total_reasoning_tokens: int
    api_latency_avg_s: float
    cache_hit_rate: float
    response_token_ratio: float
    has_reasoning: bool
    per_model_usage: PerModelUsage = Field(default_factory=PerModelUsage)


# ── Scalar helpers (internal) ──────────────────────────────────────────


def _as_int(value: object) -> int:
    """Coerce a span-attribute token value to int; non-numeric → 0.

    ``bool`` is rejected (subclasses ``int`` in Python) so a stray ``True``
    does not silently count as 1.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _span_status_is_error(span: SpanModel) -> bool:
    """Check if a span's status code indicates an error."""
    return span.status.code == SpanStatusCode.ERROR


# ── Span extraction helpers ────────────────────────────────────────────


def extract_output_text(attrs: dict[str, Any]) -> str | None:
    """Extract assistant response text from ``gen_ai.output.messages``.

    The attribute is a list of message dicts in parts-based format::

        [{"role": "assistant", "parts": [{"type": "text", "content": "..."}]}]

    Returns the first part's ``content`` of the first message, or ``None``
    if the attribute is missing or malformed. Span attributes cross a
    serialization boundary, so ``isinstance`` is the correct guard (rules 6/9).
    """
    messages = attrs.get(GenAiAttr.OUTPUT_MESSAGES.value)
    if not isinstance(messages, list) or not messages:
        return None
    first = messages[0]
    if not isinstance(first, dict):
        return None
    parts = first.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    first_part = parts[0]
    if not isinstance(first_part, dict):
        return None
    content = first_part.get("content")
    if isinstance(content, str):
        return content
    return None


def extract_final_response(spans: list[SpanModel]) -> str:
    """Extract the final assistant response from a trajectory.

    Uses the last ``chat`` span's ``gen_ai.output.messages`` (the one without
    tool_calls).  Falls back to the ``invoke_agent`` root span's
    ``langfuse.observation.output`` attribute (set by ``RootSpanHook`` to
    ``result.content``) when no tool-call-free chat span is found (the
    max-iterations case).  Returns ``""`` if that attribute is also missing
    or empty.
    """
    chat_spans = [s for s in spans if s.name == SpanName.CHAT.value]
    # Prefer the last chat span without tool_calls (the final answer).
    for s in reversed(chat_spans):
        tc = s.attributes.get(GenAiAttr.OUTPUT_TOOL_CALLS.value)
        if isinstance(tc, list) and len(tc) > 0:
            continue
        content = extract_output_text(s.attributes)
        if content:
            return content
    # Fallback: invoke_agent root span's observation output (max-iterations case).
    for s in reversed(spans):
        if s.name != SpanName.INVOKE_AGENT.value:
            continue
        output = s.attributes.get(GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT.value)
        if isinstance(output, str) and output:
            return output
    return ""


# ── Metrics ────────────────────────────────────────────────────────────


def compute_root_subtrees(spans: list[SpanModel]) -> dict[str, list[SpanModel]]:
    """Map each agent root span to the spans owned by that agent turn."""
    children: dict[str | None, list[SpanModel]] = {}
    for span in spans:
        children.setdefault(span.parent_span_id, []).append(span)

    subtrees: dict[str, list[SpanModel]] = {}
    for root in spans:
        if root.name != SpanName.INVOKE_AGENT.value:
            continue
        collected = {root.span_id}
        pending = deque([root.span_id])
        while pending:
            parent_id = pending.popleft()
            for child in children.get(parent_id, []):
                if child.span_id in collected:
                    continue
                if child.name == SpanName.INVOKE_AGENT.value:
                    continue
                collected.add(child.span_id)
                pending.append(child.span_id)
        subtrees[root.span_id] = [span for span in spans if span.span_id in collected]
    return subtrees


def compute_metrics(spans: list[SpanModel]) -> TrajectoryMetrics:
    """Compute direction-clear observability metrics for a trajectory.

    Token usage is summed from ``chat`` spans only — NOT from the
    ``invoke_agent`` root span, which carries cumulative usage that would
    double-count.
    """
    tool_spans = [s for s in spans if s.name == SpanName.EXECUTE_TOOL.value]
    chat_spans = [s for s in spans if s.name == SpanName.CHAT.value]

    total_tools = len(tool_spans)
    error_tools = sum(1 for s in tool_spans if _span_status_is_error(s))
    tool_success_rate = (total_tools - error_tools) / total_tools if total_tools > 0 else 1.0

    total_input_tokens = sum(
        _as_int(s.attributes.get(GenAiAttr.USAGE_INPUT_TOKENS.value)) for s in chat_spans
    )
    total_output_tokens = sum(
        _as_int(s.attributes.get(GenAiAttr.USAGE_OUTPUT_TOKENS.value)) for s in chat_spans
    )
    total_reasoning_tokens = sum(
        _as_int(s.attributes.get(GenAiAttr.USAGE_REASONING_TOKENS.value)) for s in chat_spans
    )

    durations = [s.end_time - s.start_time for s in chat_spans if s.end_time is not None]
    api_latency_avg_s = sum(durations) / len(durations) if durations else 0.0

    cache_read_tokens = sum(
        _as_int(s.attributes.get(GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS.value)) for s in chat_spans
    )
    # Span input_tokens is the UNCACHED count, so the prompt total the cache
    # served a fraction of is uncached + cached.
    cache_hit_rate = (
        cache_read_tokens / (total_input_tokens + cache_read_tokens)
        if total_input_tokens + cache_read_tokens > 0
        else 0.0
    )

    total_tokens = total_input_tokens + total_output_tokens
    response_token_ratio = total_output_tokens / total_tokens if total_tokens > 0 else 0.0

    iteration_count = sum(1 for s in spans if s.name == SpanName.ITERATION_START.value)

    return TrajectoryMetrics(
        tool_success_rate=tool_success_rate,
        tool_call_count=total_tools,
        error_tool_count=error_tools,
        iteration_count=iteration_count,
        llm_call_count=len(chat_spans),
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        total_reasoning_tokens=total_reasoning_tokens,
        api_latency_avg_s=api_latency_avg_s,
        cache_hit_rate=cache_hit_rate,
        response_token_ratio=response_token_ratio,
        has_reasoning=total_reasoning_tokens > 0,
    )
