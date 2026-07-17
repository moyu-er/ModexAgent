"""TraceCollectorHook — lifecycle hook that records OTel spans directly.

Constructs :class:`~modex_agent.trace.otel_store.SpanModel` values at each
lifecycle hook point (TURN_START, LLM_CALL, TOOL_BATCH, TOOL_CALL, TURN_END)
and persists them via :meth:`OtelSpanTraceStore.save_span`.  The hook owns
the per-trace ``trace_id → root span_id`` mapping so child spans link to the
root ``invoke_agent`` span via ``parent_span_id``.

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
    AfterLLMResponseHook,
    AfterToolExecutionHook,
    BeforeToolExecutionHook,
    BeforeTurnHook,
    FinallyTurnHook,
)
from modex_agent.runtime.enums import OperationKind, TurnCustomKey
from modex_agent.trace.otel_store import OtelSpanTraceStore
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
    from modex_agent.core.tool_manager import ToolResult
    from modex_agent.core.types import LLMResponse, ToolCall

logger = logging.getLogger(__name__)

# Character limit for content stored in span attributes (file-friendly).
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
    AfterLLMResponseHook,
    BeforeToolExecutionHook,
    AfterToolExecutionHook,
    FinallyTurnHook,
):
    """Collects OTel spans at each lifecycle hook point.

    Records TURN_START, LLM_CALL, TOOL_BATCH, TOOL_CALL, and TURN_END
    events with full message content (truncated for local file storage)
    into one or more configured :class:`OtelSpanTraceStore` instances.

    The hook maintains ``_root_span_ids: dict[trace_id, span_id]`` so child
    spans link to the root ``invoke_agent`` span via ``parent_span_id``.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        # trace_id → (root span_id, start_time) — set on TURN_START, consumed on TURN_END
        self._root_span_info: dict[str, tuple[str, float]] = {}

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

    def _resolve_parent(self, trace_id: str, span_id: str, kind: OperationKind) -> str | None:
        """Resolve parent_span_id and track root span_id for *trace_id*.

        - ``TURN_START``: no parent, store *span_id* + timestamp as root info.
        - Other kinds: look up the root span_id for this trace_id.
        """
        if kind == OperationKind.TURN_START:
            self._root_span_info[trace_id] = (span_id, time.time())
            return None
        info = self._root_span_info.get(trace_id)
        return info[0] if info is not None else None

    def _build_base_attrs(self, ctx: AgentContext) -> dict[str, object]:
        """Build the common attribute set carried on every span."""
        attrs: dict[str, object] = {
            GenAiAttr.AGENT_NAME: self._agent_name(ctx),
            GenAiAttr.SESSION_ID: str(ctx.session),
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
        full_attrs: dict[str, object] = self._build_base_attrs(ctx)
        op_name = operation_attr_for_kind(str(kind))
        if op_name is not None:
            full_attrs[GenAiAttr.OPERATION_NAME] = op_name
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

    async def _last_user_messages(self, ctx: AgentContext, limit: int = 3) -> list[dict[str, object]]:
        """Return the last *limit* user/assistant messages from history for context."""
        try:
            all_msgs = await ctx.history.to_list()
        except Exception:
            return []
        recent: list[dict[str, object]] = []
        for msg in reversed(list(all_msgs)[-20:]):
            if msg.role in ("user", "assistant"):
                recent.append({
                    "role": str(msg.role),
                    "content": _truncate(str(msg.content)[:2000], 2000),
                })
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

    async def after_llm_response(self, ctx: AgentContext, response: LLMResponse) -> None:
        if not self._enabled:
            return
        attrs: dict[str, object] = {
            "finish_reason": response.finish_reason,
            "has_tool_calls": response.has_tool_calls,
            GenAiAttr.OUTPUT_CONTENT: _truncate(response.content or ""),
        }
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
        if response.tool_calls:
            attrs[GenAiAttr.OUTPUT_TOOL_CALLS] = [
                {
                    "tool_name": tc.tool_name,
                    "arguments": _safe_json_dumps(tc.arguments),
                }
                for tc in response.tool_calls
            ]
        span = self._make_span(
            ctx,
            OperationKind.LLM_CALL,
            time.time(),
            attrs=attrs,
            error=response.error,
        )
        if span is not None:
            await self._save_span(span, ctx)

    async def before_tool_execution(
        self, ctx: AgentContext, tool_calls: Sequence[ToolCall]
    ) -> None:
        if not self._enabled:
            return
        attrs: dict[str, object] = {
            "tool_count": len(tool_calls),
            "tool_names": [tc.tool_name for tc in tool_calls],
            "tool_arguments": [
                {"tool_name": tc.tool_name, "arguments": _safe_json_dumps(tc.arguments)}
                for tc in tool_calls
            ],
        }
        span = self._make_span(
            ctx,
            OperationKind.TOOL_BATCH,
            time.time(),
            attrs=attrs,
        )
        if span is not None:
            await self._save_span(span, ctx)

    async def after_tool_execution(
        self, ctx: AgentContext, results: Sequence[ToolResult]
    ) -> None:
        if not self._enabled:
            return
        for result in results:
            attrs: dict[str, object] = {GenAiAttr.TOOL_NAME: result.tool_name}
            if result.result is not None:
                attrs[GenAiAttr.TOOL_RESULT] = _truncate(str(result.result))
            duration_ms: int | None = None
            if result.execution_time is not None:
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

    async def finally_turn(self, ctx: AgentContext, result: AgentResult | None) -> None:
        if not self._enabled:
            return
        trace_id = self._trace_id(ctx)
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
            if result.content is not None:
                attrs[GenAiAttr.OUTPUT_CONTENT] = _truncate(result.content)
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
            attributes={**self._build_base_attrs(ctx), GenAiAttr.OPERATION_NAME: "invoke_agent", **attrs},
            status=status,
        )
        await self._save_span(span, ctx)
