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
    PROVIDER_NAME = "gen_ai.provider.name"  # Required (replaces deprecated gen_ai.system)
    AGENT_NAME = "gen_ai.agent.name"
    AGENT_ID = "gen_ai.agent.id"
    AGENT_DESCRIPTION = "gen_ai.agent.description"
    AGENT_VERSION = "gen_ai.agent.version"
    CONVERSATION_ID = "gen_ai.conversation.id"
    INVOCATION_ID = "gen_ai.invocation.id"  # custom (not in OTel spec)

    # ── Request ───────────────────────────────────────────────────────
    REQUEST_MODEL = "gen_ai.request.model"
    REQUEST_TEMPERATURE = "gen_ai.request.temperature"
    REQUEST_MAX_TOKENS = "gen_ai.request.max_tokens"
    REQUEST_TOP_P = "gen_ai.request.top_p"
    REQUEST_FREQUENCY_PENALTY = "gen_ai.request.frequency_penalty"
    REQUEST_PRESENCE_PENALTY = "gen_ai.request.presence_penalty"
    REQUEST_STREAM = "gen_ai.request.stream"
    INPUT_MESSAGES = "gen_ai.input.messages"
    SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"  # opt-in
    SYSTEM_PROMPT_HASH = "gen_ai.system.prompt_hash"  # custom
    SYSTEM_PROMPT_LENGTH = "gen_ai.system.prompt_length"  # custom

    # ── Response ──────────────────────────────────────────────────────
    RESPONSE_MODEL = "gen_ai.response.model"
    RESPONSE_ID = "gen_ai.response.id"
    RESPONSE_FINISH_REASONS = "gen_ai.response.finish_reasons"  # string[]
    OUTPUT_MESSAGES = "gen_ai.output.messages"
    OUTPUT_REASONING_CONTENT = "gen_ai.output.reasoning_content"  # custom
    OUTPUT_TOOL_CALLS = "gen_ai.output.tool_calls"  # custom

    # ── Usage ─────────────────────────────────────────────────────────
    USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
    USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
    USAGE_REASONING_TOKENS = "gen_ai.usage.reasoning.output_tokens"
    USAGE_CACHE_READ_INPUT_TOKENS = "gen_ai.usage.cache_read.input_tokens"
    USAGE_CACHE_CREATION_INPUT_TOKENS = "gen_ai.usage.cache_creation.input_tokens"

    # ── API timing (G1) ───────────────────────────────────────────────
    API_DURATION_S = "gen_ai.api.duration_s"  # custom — LLM call wall-clock duration

    # ── Tool ──────────────────────────────────────────────────────────
    TOOL_NAME = "gen_ai.tool.name"
    TOOL_DESCRIPTION = "gen_ai.tool.description"
    TOOL_TYPE = "gen_ai.tool.type"
    TOOL_CALL_ID = "gen_ai.tool.call.id"
    TOOL_CALL_ARGUMENTS = "gen_ai.tool.call.arguments"  # opt-in
    TOOL_RESULT = "gen_ai.tool.call.result"
    TOOL_SUCCESS = "gen_ai.tool.success"  # custom
    TOOL_FAIL = "gen_ai.tool.fail"  # custom
    TOOL_ERROR_TYPE = "error.type"  # OTel standard (error.* namespace)
    TOOL_IMAGE_COUNT = "gen_ai.tool.image_count"  # custom — multimodal observability

    # ── Approval (G3) ─────────────────────────────────────────────────
    APPROVAL_DECISION = "gen_ai.approval.decision"  # custom
    APPROVAL_DENY_REASON = "gen_ai.approval.deny_reason"  # custom
    APPROVAL_TOOL_NAME = "gen_ai.approval.tool_name"  # custom
    APPROVAL_TOOL_CALL_ID = "gen_ai.approval.tool_call_id"  # custom

    # ── Iteration (G5) ────────────────────────────────────────────────
    ITERATION_NUMBER = "gen_ai.iteration.number"  # custom

    # ── Multi-agent handoff (G10) — custom ────────────────────────────
    HANDOFF_TARGET_AGENT = "gen_ai.handoff.target_agent"
    HANDOFF_TARGET_KIND = "gen_ai.handoff.target_kind"
    HANDOFF_MESSAGE_TYPE = "gen_ai.handoff.message_type"
    HANDOFF_PARENT_TURN_ID = "gen_ai.handoff.parent_turn_id"
    HANDOFF_CHILD_TURN_ID = "gen_ai.handoff.child_turn_id"

    # ── Training (custom) ─────────────────────────────────────────────
    TRAINING_RELEVANT = "gen_ai.training.relevant"

    # ── Langfuse trace-level mapping (langfuse.* namespace) ───────────
    # These map directly to Langfuse trace fields (Sessions/Users pages).
    # See https://langfuse.com/integrations/native/opentelemetry#property-mapping
    LANGFUSE_SESSION_ID = "langfuse.session.id"
    LANGFUSE_USER_ID = "langfuse.user.id"
    LANGFUSE_TRACE_NAME = "langfuse.trace.name"
    LANGFUSE_TRACE_INPUT = "langfuse.trace.input"
    LANGFUSE_TRACE_OUTPUT = "langfuse.trace.output"

    # ── Langfuse observation-level mapping ────────────────────────────
    # Langfuse maps these to the observation's input/output/model/type fields.
    # gen_ai.prompt / gen_ai.completion are the OTel GenAI semconv names that
    # Langfuse recognizes; langfuse.observation.type marks a span as a
    # "generation" so usage/cost/model fields are populated.
    GEN_AI_PROMPT = "gen_ai.prompt"
    GEN_AI_COMPLETION = "gen_ai.completion"
    LANGFUSE_OBSERVATION_TYPE = "langfuse.observation.type"
    LANGFUSE_OBSERVATION_INPUT = "langfuse.observation.input"
    LANGFUSE_OBSERVATION_OUTPUT = "langfuse.observation.output"
    LANGFUSE_OBSERVATION_LEVEL = "langfuse.observation.level"
    LANGFUSE_OBSERVATION_COMPLETION_START_TIME = "langfuse.observation.completion_start_time"

    # ── Langfuse internal (root marking) ──────────────────────────────
    LANGFUSE_INTERNAL_AS_ROOT = "langfuse.internal.as_root"


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
    ITERATION_START = "iteration.start"
    ITERATION_END = "iteration.end"
    ITERATION = "iteration"
    AGENT_HANDOFF = "agent.handoff"


class SpanKind(StrEnum):
    """OTel span kind constants (subset used by this module)."""

    INTERNAL = "INTERNAL"
    CLIENT = "CLIENT"


class SpanStatusCode(StrEnum):
    """OTel span status code constants."""

    OK = "OK"
    ERROR = "ERROR"
    UNSET = "UNSET"


class LangfuseObservationType(StrEnum):
    """Langfuse observation type values for ``langfuse.observation.type``.

    Accepted by Langfuse OTLP ingestion (priority 1 mapper — always wins
    over heuristic ``gen_ai.*`` mappers). Values are lowercase strings.
    See: langfuse/packages/shared/src/server/otel/ObservationTypeMapper.ts
    """

    SPAN = "span"
    GENERATION = "generation"
    EVENT = "event"
    AGENT = "agent"
    TOOL = "tool"
    CHAIN = "chain"


class LangfuseObservationLevel(StrEnum):
    """Langfuse observation level values for ``langfuse.observation.level``.

    See: langfuse/packages/shared/src/server/otel/attributes.ts
    (``OBSERVATION_LEVEL`` — validated against ObservationLevelDomain).
    """

    DEBUG = "DEBUG"
    DEFAULT = "DEFAULT"
    WARNING = "WARNING"
    ERROR = "ERROR"


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
