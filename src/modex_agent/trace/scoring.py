"""Agent trajectory scoring — L2 heuristics for eval and training data derivation.

Extracted from training_exporter.py to share between:
- TrainingDataExporter (SFT/DPO export scoring)
- L2ScoreInjector (Langfuse score injection, Layer 1 eval)
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from modex_agent.trace.semconv import GenAiAttr, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel

# ── Data models ────────────────────────────────────────────────────────


class TrajectoryScore(BaseModel):
    """L2 heuristic scores for one trajectory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_success_rate: float
    reasoning_depth: int
    trajectory_compactness: float


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
    tool_calls).  Falls back to the last ``chat`` span's content.
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
    # Fallback: last chat span content of any kind.
    for s in reversed(chat_spans):
        content = extract_output_text(s.attributes)
        if content:
            return content
    return ""


# ── Scoring ────────────────────────────────────────────────────────────


def compute_score(spans: list[SpanModel]) -> TrajectoryScore:
    """Compute L2 heuristic scores for a trajectory.

    - ``tool_success_rate``: non-error ``execute_tool`` spans / total
    - ``reasoning_depth``: sum of ``reasoning_tokens`` from ``chat`` spans
    - ``trajectory_compactness``: final response length / total tokens
    """
    tool_spans = [s for s in spans if s.name == SpanName.EXECUTE_TOOL.value]
    total_tools = len(tool_spans)
    successful_tools = sum(1 for s in tool_spans if not _span_status_is_error(s))
    tool_success_rate = successful_tools / total_tools if total_tools > 0 else 1.0

    chat_spans = [s for s in spans if s.name == SpanName.CHAT.value]
    reasoning_depth = 0
    total_tokens = 0
    for s in chat_spans:
        attrs = s.attributes
        reasoning_depth += _as_int(attrs.get(GenAiAttr.USAGE_REASONING_TOKENS.value))
        total_tokens += _as_int(attrs.get(GenAiAttr.USAGE_INPUT_TOKENS.value)) + _as_int(
            attrs.get(GenAiAttr.USAGE_OUTPUT_TOKENS.value)
        )

    final_content = extract_final_response(spans)
    final_len = len(final_content)
    trajectory_compactness = final_len / total_tokens if total_tokens > 0 else 0.0

    return TrajectoryScore(
        tool_success_rate=tool_success_rate,
        reasoning_depth=reasoning_depth,
        trajectory_compactness=trajectory_compactness,
    )


def overall_score(score: TrajectoryScore) -> float:
    """Combine L2 heuristics into a single 0.0–1.0 score for DPO gap filtering."""
    normalized_reasoning = min(score.reasoning_depth / 1000.0, 1.0)
    normalized_compactness = min(max(score.trajectory_compactness, 0.0), 1.0)
    return (
        0.5 * max(0.0, min(score.tool_success_rate, 1.0))
        + 0.3 * normalized_reasoning
        + 0.2 * normalized_compactness
    )


def score_to_rating(score: float) -> int:
    """Map a 0.0–1.0 overall score to a 1–5 rating."""
    if score >= 0.8:
        return 5
    if score >= 0.6:
        return 4
    if score >= 0.4:
        return 3
    if score >= 0.2:
        return 2
    return 1
