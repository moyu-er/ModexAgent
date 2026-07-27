"""TraceCollectorHook — lifecycle hook that records OTel spans directly.

Constructs :class:`~modex_agent.trace.otel_store.SpanModel` values at each
lifecycle hook point (TURN_START, LLM_CALL, TOOL_BATCH, TOOL_CALL, TURN_END,
APPROVAL, ITERATION_START, ITERATION_END) and persists them via
:meth:`OtelSpanTraceStore.save_span`.  The hook owns the per-trace
``trace_id → root span_id`` mapping so child spans link to the root
``invoke_agent`` span via ``parent_span_id``.

Implements gap remediation (G1–G5):
- G1: LLM call wall-clock duration (``before_llm`` → ``after_llm_response`` pairing).
- G2: Request prompt capture via :class:`PromptCaptureStrategy`.
- G3: ``human.review`` approval span on ``after_approval``.
- G5: ``iteration.start``/``iteration.end`` boundary spans.

Content is truncated at 4000 chars (``_CONTENT_MAX_CHARS``) for local file
friendliness; ``_ARG_MAX_CHARS`` caps tool arguments at 2000 chars.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

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

_CONTENT_MAX_CHARS = 4000
_ARG_MAX_CHARS = 2000


def _truncate(text: str, max_chars: int = _CONTENT_MAX_CHARS) -> str:
    """Return *text* truncated to *max_chars* with a truncation marker."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n\n[...truncated, {len(text) - max_chars} more chars]"


def _safe_json_dumps(obj: object, max_chars: int = _ARG_MAX_CHARS) -> str:
    """JSON-serialise *obj* and truncate if needed."""
    try:
        s = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(obj)
    return _truncate(s, max_chars)


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

    Records TURN_START, LLM_CALL, TOOL_BATCH, TOOL_CALL, APPROVAL,
    ITERATION_START, ITERATION_END, and TURN_END events with full message
    content (truncated for local file storage) into one or more configured
    :class:`OtelSpanTraceStore` instances.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        prompt_capture: PromptCaptureStrategy | None = None,
        model: str | None = None,
    ) -> None:
        self._enabled = enabled
        self._prompt_capture = prompt_capture
        self._model = model
        self._root_span_info: dict[str, tuple[str, float]] = {}
        self._llm_start_times: dict[str, float] = {}
        self._llm_request_attrs: dict[str, dict[str, object]] = {}
        self._iteration_start_times: dict[str, float] = {}
        self._tool_batch_info: dict[str, tuple[float, Sequence[ToolCall]]] = {}

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

    async def _last_user_messages(
        self, ctx: AgentContext, limit: int = 3
    ) -> list[dict[str, object]]:
        """Return the last *limit* user/assistant messages from history for context."""
        try:
            all_msgs = await ctx.history.to_list()
        except Exception:
            return []
        recent: list[dict[str, object]] = []
        for msg in reversed(list(all_msgs)[-20:]):
            if msg.role in ("user", "assistant"):
                recent.append(
                    {
                        "role": str(msg.role),
                        "content": _truncate(str(msg.content)[:2000], 2000),
                    }
                )
                if len(recent) >= limit:
                    break
        recent.reverse()
        return recent

    # -- hook implementations ------------------------------------------------

    async def before_turn(self, ctx: AgentContext) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        root_span_id = uuid.uuid4().hex
        self._root_span_info[trace_id] = (root_span_id, time.time())
        if ctx.runtime is not None:
            ctx.runtime.state.custom[TurnCustomKey.ROOT_SPAN_ID] = root_span_id

    async def before_llm(self, ctx: AgentContext, request: Sequence[ChatMessage]) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        self._llm_start_times[trace_id] = time.time()
        if self._prompt_capture is not None:
            self._llm_request_attrs[trace_id] = self._prompt_capture.capture(request, self._model)

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        now = time.time()
        start_time = self._llm_start_times.pop(trace_id, None)

        response_content = response.content or ""
        output_messages: list[dict[str, object]] = [
            {
                "role": "assistant",
                "parts": [{"type": "text", "content": _truncate(response_content)}],
            }
        ]

        attrs: dict[str, object] = {
            GenAiAttr.RESPONSE_FINISH_REASONS: [response.finish_reason.value],
            "has_tool_calls": response.has_tool_calls,
            GenAiAttr.OUTPUT_MESSAGES: output_messages,
            GenAiAttr.GEN_AI_COMPLETION: _truncate(response_content),
            GenAiAttr.LANGFUSE_OBSERVATION_TYPE: "generation",
        }

        if start_time is not None:
            duration_s = now - start_time
            attrs[GenAiAttr.API_DURATION_S] = duration_s
        if self._model is not None:
            attrs.setdefault(GenAiAttr.REQUEST_MODEL, self._model)
            attrs[GenAiAttr.RESPONSE_MODEL] = self._model

        request_attrs = self._llm_request_attrs.pop(trace_id, None)
        if request_attrs is not None:
            messages_val = request_attrs.get(GenAiAttr.INPUT_MESSAGES)
            attrs.update(request_attrs)
            if messages_val is not None:
                attrs[GenAiAttr.GEN_AI_PROMPT] = json.dumps(
                    messages_val, ensure_ascii=False, default=str
                )

        if response.reasoning_content:
            attrs[GenAiAttr.OUTPUT_REASONING_CONTENT] = _truncate(response.reasoning_content)
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
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        self._tool_batch_info[trace_id] = (time.time(), tool_calls)

    async def after_tool_execution(self, ctx: AgentContext, results: Sequence[ToolResult]) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        batch_info = self._tool_batch_info.pop(trace_id, None)

        if batch_info is not None:
            batch_start, tool_calls = batch_info
            batch_end = time.time()
            batch_attrs: dict[str, object] = {
                "tool_count": len(tool_calls),
                "tool_names": [tc.tool_name for tc in tool_calls],
                "tool_arguments": [
                    {"tool_name": tc.tool_name, "arguments": _safe_json_dumps(tc.arguments)}
                    for tc in tool_calls
                ],
            }
            batch_span = self._make_span(
                ctx,
                OperationKind.TOOL_BATCH,
                batch_start,
                duration_ms=int((batch_end - batch_start) * 1000),
                attrs=batch_attrs,
            )
            if batch_span is not None:
                await self._save_span(batch_span, ctx)

        for result in results:
            attrs: dict[str, object] = {
                GenAiAttr.TOOL_NAME: result.tool_name,
                GenAiAttr.TOOL_TYPE: "function",
                GenAiAttr.TOOL_SUCCESS: result.success,
                GenAiAttr.TOOL_FAIL: not result.success,
            }
            if result.call_id is not None:
                attrs[GenAiAttr.TOOL_CALL_ID] = result.call_id
            if result.result is not None:
                attrs[GenAiAttr.TOOL_RESULT] = _truncate(str(result.result))
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

    async def after_approval(self, ctx: AgentContext, transaction: ApprovalTransaction) -> None:
        if not self._enabled:
            return
        attrs: dict[str, object] = {
            GenAiAttr.APPROVAL_DECISION: str(transaction.status),
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
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        now = time.time()
        self._iteration_start_times[trace_id] = now
        iteration_number = self._iteration_number(ctx)
        attrs: dict[str, object] = {GenAiAttr.ITERATION_NUMBER: iteration_number}
        span = self._make_iteration_span(ctx, SpanName.ITERATION_START, now, attrs=attrs)
        if span is not None:
            await self._save_span(span, ctx)

    async def after_iteration(self, ctx: AgentContext) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        now = time.time()
        start_time = self._iteration_start_times.pop(trace_id, None)
        iteration_number = self._iteration_number(ctx)
        if iteration_number > 0:
            iteration_number -= 1
        attrs: dict[str, object] = {GenAiAttr.ITERATION_NUMBER: iteration_number}
        duration_ms: int | None = None
        if start_time is not None:
            duration_ms = int((now - start_time) * 1000)
        span = self._make_iteration_span(
            ctx,
            SpanName.ITERATION_END,
            start_time if start_time is not None else now,
            duration_ms=duration_ms,
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
        full_attrs: dict[str, object] = self._build_base_attrs(ctx, span_name.value)
        if attrs is not None:
            full_attrs.update(attrs)
        end_time: float | None = None
        if duration_ms is not None:
            end_time = timestamp + duration_ms / 1000.0
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
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
        self._llm_start_times.pop(trace_id, None)
        self._llm_request_attrs.pop(trace_id, None)
        self._iteration_start_times.pop(trace_id, None)
        self._tool_batch_info.pop(trace_id, None)
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
            "recent_messages": await self._last_user_messages(ctx),
        }
        if result is not None:
            attrs["stop_reason"] = str(result.stop_reason)
            attrs[GenAiAttr.RESPONSE_FINISH_REASONS] = [str(result.stop_reason).lower()]
            if result.content is not None:
                attrs[GenAiAttr.OUTPUT_MESSAGES] = [
                    {
                        "role": "assistant",
                        "parts": [{"type": "text", "content": _truncate(result.content)}],
                    }
                ]
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
