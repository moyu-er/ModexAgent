"""TraceCollectorHook — lifecycle hook that records OTel spans directly.

Constructs :class:`~modex_agent.trace.otel_store.SpanModel` values at each
lifecycle hook point (TURN_START, LLM_CALL, TOOL_CALL, TURN_END,
APPROVAL, ITERATION_START, ITERATION_END) and persists them via
:meth:`OtelSpanTraceStore.save_span`.  The hook owns the per-trace
``trace_id → root span_id`` mapping so child spans link to the root
``invoke_agent`` span via ``parent_span_id``.

Implements gap remediation (G1–G5):
- G1: LLM call wall-clock duration (``before_llm`` → ``after_llm_response`` pairing).
- G2: Request prompt capture via :class:`PromptCaptureStrategy`.
- G3: ``human.review`` approval span on ``after_approval``.
- G5: ``iteration.start``/``iteration.end`` boundary spans.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from modex_agent.approval.constants import ApprovalStatus
from modex_agent.hook.abc import (
    AfterApprovalHook,
    AfterIterationHook,
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    BeforeIterationHook,
    BeforeLLMHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    FinallyTurnHook,
)
from modex_agent.runtime.enums import OperationKind, TurnCustomKey
from modex_agent.trace.otel_store import OtelSpanTraceStore
from modex_agent.trace.prompt_capture import PromptCaptureStrategy
from modex_agent.trace.semconv import (
    GenAiAttr,
    LangfuseObservationLevel,
    LangfuseObservationType,
    SpanKind,
    SpanName,
    SpanStatusCode,
    operation_attr_for_kind,
    span_name_for_kind,
)
from modex_agent.trace.store import SpanModel, SpanStatus

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.emitter import AgentResult
    from modex_agent.core.message import ChatMessage
    from modex_agent.core.tool_manager import ToolResult
    from modex_agent.core.types import LLMResponse, ToolCall
    from modex_agent.runtime.models import ApprovalTransaction

logger = logging.getLogger(__name__)


def _safe_json_dumps(obj: object) -> str:
    """JSON-serialise *obj*."""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


class TraceCollectorHook(
    BeforeTurnHook,
    BeforeLLMHook,
    AfterLLMResponseHook,
    BeforeToolExecutionHook,
    AfterToolExecutionHook,
    AfterApprovalHook,
    BeforeIterationHook,
    AfterIterationHook,
    FinallyTurnHook,
):
    """Collects OTel spans at each lifecycle hook point.

    Records TURN_START, LLM_CALL, TOOL_CALL, APPROVAL,
    ITERATION_START, ITERATION_END, and TURN_END events with full message
    content (no truncation — full message text stored) into one or more configured
    :class:`OtelSpanTraceStore` instances.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        prompt_capture: PromptCaptureStrategy | None = None,
        model: str | None = None,
        provider_name: str | None = None,
        request_params: dict[str, object] | None = None,
    ) -> None:
        self._enabled = enabled
        self._prompt_capture = prompt_capture
        self._model = model
        self._provider_name = provider_name
        self._request_params = request_params
        self._root_span_info: dict[str, tuple[str, float]] = {}
        self._llm_start_times: dict[str, float] = {}
        self._llm_request_attrs: dict[str, dict[str, object]] = {}
        self._iteration_start_times: dict[str, float] = {}
        self._tool_batch_info: dict[str, tuple[float, Sequence[ToolCall]]] = {}
        self._turn_usage: dict[str, dict[str, int]] = {}

    @property
    def name(self) -> str:
        return "trace_collector"

    # -- helpers -------------------------------------------------------------

    def _trace_id(self, ctx: AgentContext) -> str:
        """Return existing trace_id from turn state or generate a new one."""
        if ctx.runtime is None:
            return uuid.uuid4().hex
        tid = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        if tid is not None:
            return str(tid)
        new_id = uuid.uuid4().hex
        ctx.runtime.state.custom[TurnCustomKey.TRACE_ID] = new_id
        return new_id

    async def _save_span(self, span: SpanModel, ctx: AgentContext) -> None:
        """Persist a span to the runtime trace store, logging failures.

        The store is resolved per turn from
        ``ctx.runtime.services.trace_store`` (the workspace-rooted store
        wired by the pipeline).
        """
        runtime_store: OtelSpanTraceStore | None = (
            ctx.runtime.services.trace_store if ctx.runtime is not None else None
        )
        if runtime_store is None:
            return
        try:
            await runtime_store.save_span(span)
        except Exception:
            logger.warning(
                "TraceCollectorHook failed to save to %s",
                type(runtime_store).__name__,
                exc_info=True,
            )

    def _agent_name(self, ctx: AgentContext) -> str:
        """Return agent name from session."""
        return ctx.session.agent_name if ctx.session else "unknown"

    def _invocation_id(self, ctx: AgentContext) -> str | None:
        """Return invocation_id from session metadata."""
        if ctx.session is not None:
            return str(ctx.session.metadata.get("invocation_id", "")) or None
        return None

    def _user_id(self, ctx: AgentContext) -> str:
        """Return user identifier for Langfuse Users page.

        TEMPORARY: framework has no first-class user concept. Reads
        ``metadata['user_id']`` if the business layer sets it; falls back
        to ``"default"`` so Langfuse Users page is populated. Replace when
        AgentContext gains a typed ``user_id`` field.
        """
        if ctx.session is not None:
            uid = str(ctx.session.metadata.get("user_id", ""))
            if uid:
                return uid
        return "default"

    def _resolve_parent(self, trace_id: str, span_id: str, kind: OperationKind) -> str | None:
        """Resolve parent_span_id and track root span_id for *trace_id*.

        - ``TURN_START``: no parent, store *span_id* + timestamp as root info.
        - Other kinds: look up the root span_id for this trace_id.
        """
        if kind == OperationKind.TURN_START:
            self._root_span_info[trace_id] = (span_id, time.time())
            return None
        return self._root_span_id(trace_id)

    def _root_span_id(self, trace_id: str) -> str | None:
        """Return the root span_id for *trace_id*, or ``None`` if not yet set."""
        info = self._root_span_info.get(trace_id)
        return info[0] if info is not None else None

    def _build_base_attrs(self, ctx: AgentContext, operation_name: str) -> dict[str, object]:
        """Build the common attribute set carried on every span.

        *operation_name* is the ``gen_ai.operation.name`` value for the span
        type (e.g. ``"chat"``, ``"execute_tool"``, ``"invoke_agent"``).
        """
        attrs: dict[str, object] = {
            GenAiAttr.AGENT_NAME: self._agent_name(ctx),
            GenAiAttr.OPERATION_NAME: operation_name,
            GenAiAttr.CONVERSATION_ID: str(ctx.session),
            GenAiAttr.LANGFUSE_SESSION_ID: str(ctx.session),
            GenAiAttr.LANGFUSE_USER_ID: self._user_id(ctx),
        }
        turn_id = ctx.identity.turn_id if ctx.identity else None
        if turn_id is not None:
            attrs[GenAiAttr.LANGFUSE_TRACE_NAME] = f"{ctx.session}.{turn_id}"
        if self._provider_name is not None:
            attrs[GenAiAttr.PROVIDER_NAME] = self._provider_name
        inv = self._invocation_id(ctx)
        if inv is not None:
            attrs[GenAiAttr.INVOCATION_ID] = inv
        return attrs

    def _make_span(
        self,
        ctx: AgentContext,
        kind: OperationKind,
        timestamp: float,
        *,
        duration_ms: int | None = None,
        attrs: dict[str, object] | None = None,
        error: str | None = None,
    ) -> SpanModel | None:
        """Construct a :class:`SpanModel` for *kind*.

        Returns ``None`` for ``TURN_END`` — the root span is already written
        on ``TURN_START``; ``TURN_END`` carries no new span.
        """
        span_name = span_name_for_kind(str(kind))
        if span_name is None:
            return None

        trace_id = self._trace_id(ctx)
        span_id = uuid.uuid4().hex
        parent_span_id = self._resolve_parent(trace_id, span_id, kind)

        # ── Status ────────────────────────────────────────────────────
        if kind == OperationKind.ERROR or error is not None:
            status = SpanStatus(code=SpanStatusCode.ERROR, message=error)
        else:
            status = SpanStatus(code=SpanStatusCode.OK)

        # ── Attributes ────────────────────────────────────────────────
        op_name = operation_attr_for_kind(str(kind))
        # _make_span only reaches here for kinds with a span_name (the early
        # return above filters the rest); those kinds always have a matching
        # operation name, so op_name is non-None in practice.
        full_attrs: dict[str, object] = self._build_base_attrs(ctx, op_name or "")
        if attrs is not None:
            full_attrs.update(attrs)

        # ── Span kind ─────────────────────────────────────────────────
        span_kind = (
            SpanKind.CLIENT.value if kind == OperationKind.LLM_CALL else SpanKind.INTERNAL.value
        )

        # ── Timing ────────────────────────────────────────────────────
        start_time = timestamp
        end_time: float | None = None
        if duration_ms is not None:
            end_time = start_time + duration_ms / 1000.0

        return SpanModel(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=span_name.value,
            kind=span_kind,
            start_time=start_time,
            end_time=end_time,
            attributes=full_attrs,
            status=status,
        )

    async def _last_user_input(self, ctx: AgentContext) -> list[dict[str, object]]:
        """Return the last triggering message in parts-based format for trace input.

        For main agent: the last user message.
        For subagent: the last agent message (inbox-delivered <agent_message>).
        Preserves the original role — does not convert agent→user.
        """
        try:
            all_msgs = await ctx.history.to_list()
        except Exception:
            return []
        for msg in reversed(list(all_msgs)[-20:]):
            if msg.role in ("user", "agent"):
                return [
                    {
                        "role": str(msg.role),
                        "parts": [{"type": "text", "content": str(msg.content)}],
                    }
                ]
        return []

    # -- hook implementations ------------------------------------------------

    async def before_turn(self, ctx: AgentContext) -> None:
        """BEFORE_TURN hook — emit initial root span (invoke_agent).

        Creates the trace's root span with:
        - langfuse.internal.as_root=true (marks root observation in Langfuse)
        - langfuse.observation.input (trigger message — user or agent role)
        - langfuse.trace.input (same as obs.input, populates trace-level field)
        - langfuse.trace.name ({session_id}.{turn_id})

        finally_turn re-emits the same span_id with output + duration + usage.
        Langfuse's ClickHouse ReplacingMergeTree keeps the latest row, so
        finally_turn's output is preserved (both emissions must carry input
        to survive the full-row overwrite).
        """
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        root_span_id = uuid.uuid4().hex
        start_time = time.time()
        self._root_span_info[trace_id] = (root_span_id, start_time)
        if ctx.runtime is not None:
            ctx.runtime.state.custom[TurnCustomKey.ROOT_SPAN_ID] = root_span_id
        user_input = await self._last_user_input(ctx)
        root_attrs: dict[str, object] = {
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.AGENT.value,
            GenAiAttr.LANGFUSE_INTERNAL_AS_ROOT: True,
        }
        if user_input:
            input_json = json.dumps(user_input, ensure_ascii=False, default=str)
            root_attrs[GenAiAttr.LANGFUSE_OBSERVATION_INPUT] = input_json
            root_attrs[GenAiAttr.LANGFUSE_TRACE_INPUT] = input_json
        root_span = SpanModel(
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            name=SpanName.INVOKE_AGENT.value,
            kind=SpanKind.INTERNAL.value,
            start_time=start_time,
            end_time=start_time + 0.001,
            attributes={**self._build_base_attrs(ctx, "invoke_agent"), **root_attrs},
            status=SpanStatus(code=SpanStatusCode.OK),
        )
        await self._save_span(root_span, ctx)

    async def before_llm(self, ctx: AgentContext, request: Sequence[ChatMessage]) -> None:
        """BEFORE_LLM hook — cache LLM request for after_llm_response.

        Captures the request messages via PromptCaptureStrategy and stores
        them in _llm_request_attrs for the chat span's gen_ai.input.messages
        and langfuse.observation.input.
        """
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        self._llm_start_times[trace_id] = time.time()
        if self._prompt_capture is not None:
            captured = self._prompt_capture.capture(request, self._model)
            self._llm_request_attrs[trace_id] = captured

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        """AFTER_LLM_RESPONSE hook — emit chat span (GENERATION).

        Span attributes:
        - gen_ai.request.model / gen_ai.response.model
        - gen_ai.request.temperature / max_tokens / stream
        - gen_ai.input.messages / gen_ai.output.messages (parts-based)
        - gen_ai.prompt / gen_ai.completion (Langfuse compat strings)
        - gen_ai.usage.* (input/output/cache_read/cache_creation tokens)
        - gen_ai.response.finish_reasons
        - langfuse.observation.type=generation (priority-1 mapper)
        - langfuse.observation.input / output (JSON strings)

        Tool calls are included as parts in output_messages even when text
        content is empty (finish_reason=tool_calls).
        """
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        now = time.time()
        start_time = self._llm_start_times.pop(trace_id, None)

        response_content = response.content or ""
        output_parts: list[dict[str, object]] = [{"type": "text", "content": response_content}]
        if response.tool_calls:
            output_parts.extend(
                [
                    {
                        "type": "tool_call",
                        "name": tc.tool_name,
                        "arguments": _safe_json_dumps(tc.arguments),
                    }
                    for tc in response.tool_calls
                ]
            )
        output_messages: list[dict[str, object]] = [
            {"role": "assistant", "parts": output_parts}
        ]

        attrs: dict[str, object] = {
            GenAiAttr.RESPONSE_FINISH_REASONS: [response.finish_reason.value],
            "has_tool_calls": response.has_tool_calls,
            GenAiAttr.OUTPUT_MESSAGES: output_messages,
            GenAiAttr.GEN_AI_COMPLETION: response_content,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.GENERATION.value,
            GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT: json.dumps(
                output_messages, ensure_ascii=False, default=str
            ),
        }

        if start_time is not None:
            duration_s = now - start_time
            attrs[GenAiAttr.API_DURATION_S] = duration_s
        if self._model is not None:
            attrs.setdefault(GenAiAttr.REQUEST_MODEL, self._model)
            attrs[GenAiAttr.RESPONSE_MODEL] = self._model
        if self._request_params is not None:
            if "temperature" in self._request_params:
                attrs[GenAiAttr.REQUEST_TEMPERATURE] = self._request_params["temperature"]
            if "max_tokens" in self._request_params:
                attrs[GenAiAttr.REQUEST_MAX_TOKENS] = self._request_params["max_tokens"]
            if "top_p" in self._request_params:
                attrs[GenAiAttr.REQUEST_TOP_P] = self._request_params["top_p"]
            if "stream" in self._request_params:
                attrs[GenAiAttr.REQUEST_STREAM] = self._request_params["stream"]

        request_attrs = self._llm_request_attrs.pop(trace_id, None)
        if request_attrs is not None:
            messages_val = request_attrs.get(GenAiAttr.INPUT_MESSAGES)
            attrs.update(request_attrs)
            if messages_val is not None:
                attrs[GenAiAttr.GEN_AI_PROMPT] = json.dumps(
                    messages_val, ensure_ascii=False, default=str
                )
                attrs[GenAiAttr.LANGFUSE_OBSERVATION_INPUT] = json.dumps(
                    messages_val, ensure_ascii=False, default=str
                )

        if response.reasoning_content:
            attrs[GenAiAttr.OUTPUT_REASONING_CONTENT] = response.reasoning_content
        if response.usage:
            usage = response.usage
            input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
            if input_tokens is not None:
                attrs[GenAiAttr.USAGE_INPUT_TOKENS] = input_tokens
            output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
            if output_tokens is not None:
                attrs[GenAiAttr.USAGE_OUTPUT_TOKENS] = output_tokens
            if "reasoning_tokens" in usage:
                attrs[GenAiAttr.USAGE_REASONING_TOKENS] = usage["reasoning_tokens"]
            if "cache_read_input_tokens" in usage:
                attrs[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS] = usage["cache_read_input_tokens"]
            if "cache_creation_input_tokens" in usage:
                attrs[GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS] = usage[
                    "cache_creation_input_tokens"
                ]
            turn_usage = self._turn_usage.setdefault(
                trace_id,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "reasoning_tokens": 0,
                },
            )
            if input_tokens is not None:
                turn_usage["input_tokens"] += input_tokens
            if output_tokens is not None:
                turn_usage["output_tokens"] += output_tokens
            if "reasoning_tokens" in usage:
                turn_usage["reasoning_tokens"] += usage["reasoning_tokens"]
            if "cache_read_input_tokens" in usage:
                turn_usage["cache_read_input_tokens"] += usage["cache_read_input_tokens"]
            if "cache_creation_input_tokens" in usage:
                turn_usage["cache_creation_input_tokens"] += usage["cache_creation_input_tokens"]
        if response.tool_calls:
            attrs[GenAiAttr.OUTPUT_TOOL_CALLS] = [
                {
                    "tool_name": tc.tool_name,
                    "arguments": _safe_json_dumps(tc.arguments),
                }
                for tc in response.tool_calls
            ]

        duration_ms: int | None = None
        if start_time is not None:
            duration_ms = int((now - start_time) * 1000)
        span = self._make_span(
            ctx,
            OperationKind.LLM_CALL,
            start_time if start_time is not None else now,
            attrs=attrs,
            duration_ms=duration_ms,
            error=response.error,
        )
        if span is not None:
            await self._save_span(span, ctx)

    async def before_tool_execution(
        self, ctx: AgentContext, tool_calls: Sequence[ToolCall]
    ) -> None:
        """BEFORE_TOOL_EXECUTION hook — cache tool_calls for handoff detection.

        Stores tool_calls (with arguments) for:
        - after_tool_execution's per-tool execute_tool span input (arguments)
        - _maybe_emit_handoff_spans's agent.handoff span when tool_name=send_to_agent
        """
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        self._tool_batch_info[trace_id] = (time.time(), tool_calls)

    async def after_tool_execution(self, ctx: AgentContext, results: Sequence[ToolResult]) -> None:
        """AFTER_TOOL_EXECUTION hook — emit per-tool spans + handoff detection.

        Emits:
        1. execute_tool (TOOL): one per tool result, with:
           - gen_ai.tool.name / type / call.id / call.result
           - langfuse.observation.input = {tool_name, arguments}
           - langfuse.observation.output = {result}
        2. agent.handoff (SPAN): when tool_name=send_to_agent
        """
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        batch_info = self._tool_batch_info.pop(trace_id, None)
        tool_calls: Sequence[ToolCall] = ()
        if batch_info is not None:
            tool_calls = batch_info[1]

        tool_calls_list: list[ToolCall] = list(tool_calls)
        for idx, result in enumerate(results):
            tc_args: dict[str, object] = {}
            if idx < len(tool_calls_list):
                tc_args = dict(tool_calls_list[idx].arguments)
            attrs: dict[str, object] = {
                GenAiAttr.TOOL_NAME: result.tool_name,
                GenAiAttr.TOOL_TYPE: "function",
                GenAiAttr.TOOL_SUCCESS: result.success,
                GenAiAttr.TOOL_FAIL: not result.success,
                GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.TOOL.value,
            }
            if result.call_id is not None:
                attrs[GenAiAttr.TOOL_CALL_ID] = result.call_id
            attrs[GenAiAttr.LANGFUSE_OBSERVATION_INPUT] = json.dumps(
                {"tool_name": result.tool_name, "arguments": tc_args},
                ensure_ascii=False,
                default=str,
            )
            if result.result is not None:
                attrs[GenAiAttr.TOOL_RESULT] = str(result.result)
                attrs[GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT] = json.dumps(
                    {"result": str(result.result)},
                    ensure_ascii=False,
                    default=str,
                )
            if result.error is not None:
                attrs[GenAiAttr.TOOL_ERROR_TYPE] = result.error
            duration_ms: int | None = None
            if result.execution_time is not None and result.execution_time > 0:
                duration_ms = int(result.execution_time * 1000)
            span = self._make_span(
                ctx,
                OperationKind.TOOL_CALL,
                time.time(),
                attrs=attrs,
                duration_ms=duration_ms,
                error=result.error,
            )
            if span is not None:
                await self._save_span(span, ctx)

        if batch_info is not None:
            _, tool_calls = batch_info
            await self._maybe_emit_handoff_spans(ctx, tool_calls, results)

    async def _maybe_emit_handoff_spans(
        self,
        ctx: AgentContext,
        tool_calls: Sequence[ToolCall],
        results: Sequence[ToolResult],
    ) -> None:
        """Emit agent.handoff spans for send_to_agent tool calls.

        When an agent calls send_to_agent, this method emits an
        agent.handoff span (SPAN) on the sender's trace tree, marking
        the control transfer point. Attributes:
        - gen_ai.handoff.target_agent / target_kind / message_type
        - gen_ai.handoff.parent_turn_id (sender's turn_id)
        - langfuse.observation.input = {target_agent, content}
        - langfuse.observation.output = {result, success}

        This replaces the previous hardcoded _emit_handoff_span in
        AgentCommunicationService — trace logic now lives entirely in hooks.
        """
        for tc, result in zip(tool_calls, results):
            if tc.tool_name != "send_to_agent":
                continue
            target_agent = tc.arguments.get("target_agent", "unknown")
            root_span_id = self._root_span_id(self._trace_id(ctx))
            now = time.time()
            base = self._build_base_attrs(ctx, "invoke_agent")
            base.update({
                GenAiAttr.HANDOFF_TARGET_AGENT: target_agent,
                GenAiAttr.HANDOFF_TARGET_KIND: "unknown",
                GenAiAttr.HANDOFF_MESSAGE_TYPE: "unknown",
                GenAiAttr.HANDOFF_PARENT_TURN_ID: (
                    ctx.identity.turn_id if ctx.identity else None
                ),
                GenAiAttr.HANDOFF_CHILD_TURN_ID: None,
                GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.SPAN.value,
                GenAiAttr.LANGFUSE_OBSERVATION_INPUT: json.dumps(
                    {"target_agent": target_agent, "content": _safe_json_dumps(tc.arguments.get("content", ""))},
                    ensure_ascii=False,
                    default=str,
                ),
                GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT: json.dumps(
                    {"result": str(result.result) if result.result is not None else None, "success": result.success},
                    ensure_ascii=False,
                    default=str,
                ),
            })
            span = SpanModel(
                trace_id=self._trace_id(ctx),
                span_id=uuid.uuid4().hex,
                parent_span_id=root_span_id,
                name=SpanName.AGENT_HANDOFF.value,
                kind=SpanKind.INTERNAL.value,
                start_time=now,
                end_time=now,
                attributes=base,
                status=SpanStatus(code=SpanStatusCode.OK),
            )
            await self._save_span(span, ctx)

    async def after_approval(self, ctx: AgentContext, transaction: ApprovalTransaction) -> None:
        """AFTER_APPROVAL hook — emit human.review span (EVENT).

        Records approval decisions:
        - langfuse.observation.type=event
        - langfuse.observation.level=WARNING (denied) or DEFAULT (approved)
        - gen_ai.approval.decision / deny_reason / tool_name / tool_call_id
        """
        if not self._enabled:
            return
        attrs: dict[str, object] = {
            GenAiAttr.APPROVAL_DECISION: str(transaction.status),
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.EVENT.value,
            GenAiAttr.LANGFUSE_OBSERVATION_LEVEL: (
                LangfuseObservationLevel.WARNING.value
                if transaction.status == ApprovalStatus.DENIED
                else LangfuseObservationLevel.DEFAULT.value
            ),
        }
        if transaction.deny_reason is not None:
            attrs[GenAiAttr.APPROVAL_DENY_REASON] = transaction.deny_reason
        if transaction.requests:
            req = transaction.requests[0]
            attrs[GenAiAttr.APPROVAL_TOOL_NAME] = req.tool_name
            attrs[GenAiAttr.APPROVAL_TOOL_CALL_ID] = req.tool_call_id
        span = self._make_span(
            ctx,
            OperationKind.APPROVAL,
            time.time(),
            attrs=attrs,
        )
        if span is not None:
            await self._save_span(span, ctx)

    async def before_iteration(self, ctx: AgentContext) -> None:
        """BEFORE_ITERATION hook — cache iteration start time + emit iteration.start span.

        Emits iteration.start (SPAN) with:
        - gen_ai.iteration.number
        - langfuse.observation.input = {iteration: N}
        Does NOT set gen_ai.operation.name (iteration is not a GenAI operation).
        """
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        now = time.time()
        self._iteration_start_times[trace_id] = now
        iteration_number = self._iteration_number(ctx)
        attrs: dict[str, object] = {
            GenAiAttr.ITERATION_NUMBER: iteration_number,
            GenAiAttr.LANGFUSE_OBSERVATION_INPUT: json.dumps(
                {"iteration": iteration_number}, ensure_ascii=False, default=str
            ),
        }
        span = self._make_iteration_span(ctx, SpanName.ITERATION_START, now, attrs=attrs)
        if span is not None:
            await self._save_span(span, ctx)

    async def after_iteration(self, ctx: AgentContext) -> None:
        """AFTER_ITERATION hook — emit iteration.end span.

        Emits iteration.end (SPAN) with:
        - gen_ai.iteration.number
        - langfuse.observation.input = {iteration: N}
        - langfuse.observation.output = {iteration: N, duration_ms: ...}
        start_time = now (not before_iteration's time) to ensure correct
        chronological ordering in Langfuse (iteration.end always after
        iteration.start).
        """
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        now = time.time()
        start_time = self._iteration_start_times.pop(trace_id, None)
        iteration_number = self._iteration_number(ctx)
        if iteration_number > 0:
            iteration_number -= 1
        attrs: dict[str, object] = {
            GenAiAttr.ITERATION_NUMBER: iteration_number,
            GenAiAttr.LANGFUSE_OBSERVATION_INPUT: json.dumps(
                {"iteration": iteration_number}, ensure_ascii=False, default=str
            ),
        }
        if start_time is not None:
            duration_ms = int((now - start_time) * 1000)
            attrs[GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT] = json.dumps(
                {"iteration": iteration_number, "duration_ms": duration_ms},
                ensure_ascii=False,
                default=str,
            )
        span = self._make_iteration_span(
            ctx,
            SpanName.ITERATION_END,
            now,
            attrs=attrs,
        )
        if span is not None:
            await self._save_span(span, ctx)

    def _iteration_number(self, ctx: AgentContext) -> int:
        if ctx.runtime is None:
            return 0
        return int(getattr(ctx.runtime.state, "iteration", 0))

    def _make_iteration_span(
        self,
        ctx: AgentContext,
        span_name: SpanName,
        timestamp: float,
        *,
        duration_ms: int | None = None,
        attrs: dict[str, object] | None = None,
    ) -> SpanModel | None:
        trace_id = self._trace_id(ctx)
        span_id = uuid.uuid4().hex
        parent_span_id = self._root_span_id(trace_id)
        full_attrs: dict[str, object] = self._build_base_attrs(ctx, "")
        full_attrs.pop(GenAiAttr.OPERATION_NAME, None)
        full_attrs[GenAiAttr.LANGFUSE_OBSERVATION_TYPE] = LangfuseObservationType.SPAN.value
        if attrs is not None:
            full_attrs.update(attrs)
        end_time: float = timestamp if duration_ms is None else timestamp + duration_ms / 1000.0
        return SpanModel(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=span_name.value,
            kind=SpanKind.INTERNAL.value,
            start_time=timestamp,
            end_time=end_time,
            attributes=full_attrs,
            status=SpanStatus(code=SpanStatusCode.OK),
        )

    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None:
        """FINALLY_TURN hook — emit completed root span (invoke_agent).

        Re-emits the root span (same span_id as before_turn) with:
        - langfuse.observation.output (final assistant reply)
        - langfuse.trace.output (same as obs.output)
        - langfuse.observation.input (re-sent — Langfuse last-write-wins
          overwrites the before_turn row, so input must be re-included)
        - gen_ai.usage.* (aggregated across all LLM calls in this turn)
        - gen_ai.response.finish_reasons
        - end_time (full turn duration)

        Also cleans up per-turn state (_turn_usage, _iteration_start_times,
        _tool_batch_info).
        """
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        self._llm_start_times.pop(trace_id, None)
        self._llm_request_attrs.pop(trace_id, None)
        self._iteration_start_times.pop(trace_id, None)
        self._tool_batch_info.pop(trace_id, None)
        turn_usage = self._turn_usage.pop(trace_id, None)
        if ctx.runtime is not None:
            ctx.runtime.state.custom.pop(TurnCustomKey.ROOT_SPAN_ID, None)
        root_info = self._root_span_info.pop(trace_id, None)
        if root_info is None:
            return
        root_span_id, start_time = root_info
        end_time = time.time()
        error: str | None = None
        if result is not None and result.error:
            error = result.error
        attrs: dict[str, object] = {
            "turn_id": ctx.identity.turn_id if ctx.identity else None,
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: LangfuseObservationType.AGENT.value,
            GenAiAttr.LANGFUSE_INTERNAL_AS_ROOT: True,
        }
        user_input = await self._last_user_input(ctx)
        if user_input:
            input_json = json.dumps(user_input, ensure_ascii=False, default=str)
            attrs[GenAiAttr.LANGFUSE_OBSERVATION_INPUT] = input_json
            attrs[GenAiAttr.LANGFUSE_TRACE_INPUT] = input_json
        if result is not None:
            attrs["stop_reason"] = str(result.stop_reason)
            attrs[GenAiAttr.RESPONSE_FINISH_REASONS] = [str(result.stop_reason).lower()]
            if result.content is not None:
                output_messages = [
                    {
                        "role": "assistant",
                        "parts": [{"type": "text", "content": result.content}],
                    }
                ]
                output_json = json.dumps(output_messages, ensure_ascii=False, default=str)
                attrs[GenAiAttr.OUTPUT_MESSAGES] = output_messages
                attrs[GenAiAttr.LANGFUSE_OBSERVATION_OUTPUT] = output_json
                attrs[GenAiAttr.LANGFUSE_TRACE_OUTPUT] = output_json
        if turn_usage is not None:
            input_total = turn_usage.get("input_tokens", 0)
            if input_total > 0:
                attrs[GenAiAttr.USAGE_INPUT_TOKENS] = input_total
            output_total = turn_usage.get("output_tokens", 0)
            if output_total > 0:
                attrs[GenAiAttr.USAGE_OUTPUT_TOKENS] = output_total
            cache_read = turn_usage.get("cache_read_input_tokens", 0)
            if cache_read > 0:
                attrs[GenAiAttr.USAGE_CACHE_READ_INPUT_TOKENS] = cache_read
            cache_creation = turn_usage.get("cache_creation_input_tokens", 0)
            if cache_creation > 0:
                attrs[GenAiAttr.USAGE_CACHE_CREATION_INPUT_TOKENS] = cache_creation
            reasoning = turn_usage.get("reasoning_tokens", 0)
            if reasoning > 0:
                attrs[GenAiAttr.USAGE_REASONING_TOKENS] = reasoning
        status = (
            SpanStatus(code=SpanStatusCode.ERROR, message=error)
            if error is not None
            else SpanStatus(code=SpanStatusCode.OK)
        )
        span = SpanModel(
            trace_id=trace_id,
            span_id=root_span_id,
            parent_span_id=None,
            name=SpanName.INVOKE_AGENT.value,
            kind=SpanKind.INTERNAL.value,
            start_time=start_time,
            end_time=end_time,
            attributes={
                **self._build_base_attrs(ctx, "invoke_agent"),
                **attrs,
            },
            status=status,
        )
        await self._save_span(span, ctx)
