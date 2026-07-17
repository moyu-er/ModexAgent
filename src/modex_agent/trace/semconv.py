"""OpenTelemetry semantic convention constants for gen_ai.* attributes.

Centralizes all ``gen_ai.*`` attribute names and span names so they are not
scattered as raw strings across the OTel store.  Follows the OpenTelemetry
GenAI semantic conventions (``gen_ai.*`` namespace).

These constants are used by :mod:`modex_agent.trace.otel_store` and
:mod:`modex_agent.trace.hooks` when constructing OTel-compatible span JSON.
"""

from __future__ import annotations

from enum import StrEnum


class GenAiAttr(StrEnum):
    """``gen_ai.*`` attribute name constants."""

    # ── Operation / agent identification ──────────────────────────────
    OPERATION_NAME = "gen_ai.operation.name"
    AGENT_NAME = "gen_ai.agent.name"
    SESSION_ID = "gen_ai.session.id"
    INVOCATION_ID = "gen_ai.invocation.id"  # custom (not in OTel spec)

    # ── Output ────────────────────────────────────────────────────────
    OUTPUT_CONTENT = "gen_ai.output.content"
    OUTPUT_REASONING_CONTENT = "gen_ai.output.reasoning_content"
    OUTPUT_TOOL_CALLS = "gen_ai.output.tool_calls"

    # ── Usage ─────────────────────────────────────────────────────────
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    USAGE_REASONING_TOKENS = "gen_ai.usage.reasoning_tokens"  # custom

    # ── Tool ──────────────────────────────────────────────────────────
    TOOL_NAME = "gen_ai.tool.name"
    TOOL_RESULT = "gen_ai.tool.result"

    # ── Training (custom) ─────────────────────────────────────────────
    TRAINING_RELEVANT = "gen_ai.training.relevant"


class SpanName(StrEnum):
    """Span names for each operation kind."""

    INVOKE_AGENT = "invoke_agent"
    CHAT = "chat"
    EXECUTE_TOOL_BATCH = "execute_tool_batch"
    EXECUTE_TOOL = "execute_tool"
    HUMAN_REVIEW = "human.review"
    CONTROL_COMMAND = "control_command"
    ERROR = "error"
    TRAINING_TAG = "training_tag"


class SpanKind(StrEnum):
    """OTel span kind constants (subset used by this module)."""

    INTERNAL = "INTERNAL"
    CLIENT = "CLIENT"


class SpanStatusCode(StrEnum):
    """OTel span status code constants."""

    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


# ── OperationKind → SpanName mapping ─────────────────────────────────
# Centralized so the conversion logic in otel_store never uses raw strings.
_OPERATION_SPAN_NAMES: dict[str, SpanName] = {
    "turn_start": SpanName.INVOKE_AGENT,
    "llm_call": SpanName.CHAT,
    "tool_batch": SpanName.EXECUTE_TOOL_BATCH,
    "tool_call": SpanName.EXECUTE_TOOL,
    "approval": SpanName.HUMAN_REVIEW,
    "control_command": SpanName.CONTROL_COMMAND,
    "error": SpanName.ERROR,
    # turn_end does not produce a new span (root already written on turn_start)
}


def span_name_for_kind(kind_value: str) -> SpanName | None:
    """Return the span name for an OperationKind value, or ``None`` if no span."""
    return _OPERATION_SPAN_NAMES.get(kind_value)


# ── gen_ai.operation.name attribute value mapping ────────────────────
_OPERATION_ATTR_VALUES: dict[str, str] = {
    "turn_start": "invoke_agent",
    "llm_call": "chat",
    "tool_batch": "execute_tool_batch",
    "tool_call": "execute_tool",
    "approval": "human_review",
    "control_command": "control_command",
    "error": "error",
}


def operation_attr_for_kind(kind_value: str) -> str | None:
    """Return the ``gen_ai.operation.name`` value for an OperationKind, or ``None``."""
    return _OPERATION_ATTR_VALUES.get(kind_value)
