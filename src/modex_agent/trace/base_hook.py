"""BaseTraceHook -- shared infrastructure for all trace span hooks.

Provides common helpers for the specialized trace hook classes without
duplicating span construction, persistence, or attribute handling.

Not an ABC -- it is a concrete base with helper methods. Subclasses inherit
this and add their specific hook ABC(s) (BeforeGraphHook, BeforeLLMHook,
etc.).
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.semconv import GenAiAttr
from modex_agent.trace.store import SpanModel, SpanStatus

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.trace.otel_store import OtelSpanTraceStore
    from modex_agent.trace.score_injector import L2ScoreInjector
    from modex_agent.trace.session_state import TraceSessionState

logger = logging.getLogger(__name__)


class BaseTraceHook:
    """Shared base for all trace span hooks.

    Provides common infrastructure: span construction/persistence, base
    attribute building, trace/root span ID resolution, and user input
    extraction. Subclasses inherit this and add their specific hook ABC(s).

    Shared mutable trace state is centralized in
    :class:`~modex_agent.trace.session_state.TraceSessionState`, injected
    via the ``session`` constructor parameter and accessible as
    ``self._session``.
    """

    def __init__(
        self,
        *,
        session: TraceSessionState,
        store: OtelSpanTraceStore | None,
        model: str | None = None,
        provider_name: str | None = None,
        request_params: dict[str, object] | None = None,
        score_injector: L2ScoreInjector | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._model = model
        self._provider_name = provider_name
        self._request_params = request_params
        self._score_injector = score_injector

    @property
    def _enabled(self) -> bool:
        """True when a trace store is configured."""
        return self._store is not None

    def _new_span_id(self) -> str:
        """Generate a 16-character hex span ID."""
        return uuid.uuid4().hex[:16]

    def _trace_id(self, ctx: AgentContext) -> str:
        """Return the trace_id from turn state, or generate a new one.

        Reads ``ctx.runtime.state.custom[TurnCustomKey.TRACE_ID]``. If
        present, returns it. If absent, generates a new ``uuid4().hex``
        but does NOT store it -- storing is RootSpanHook's job.
        """
        if ctx.runtime is None:
            return uuid.uuid4().hex
        tid = ctx.runtime.state.custom.get(TurnCustomKey.TRACE_ID)
        if tid is not None:
            return str(tid)
        return uuid.uuid4().hex

    def _root_span_id(self, ctx: AgentContext) -> str | None:
        """Return the root span_id from turn state, or ``None`` if not set."""
        if ctx.runtime is None:
            return None
        sid = ctx.runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
        if sid is not None:
            return str(sid)
        return None

    def _agent_name(self, ctx: AgentContext) -> str:
        """Return agent name from session, or ``"agent"`` as fallback."""
        return ctx.session.agent_name if ctx.session else "agent"

    def _user_id(self, ctx: AgentContext) -> str | None:
        """Return user identifier from session metadata, or ``None``."""
        if ctx.session is not None:
            uid = str(ctx.session.metadata.get("user_id", ""))
            if uid:
                return uid
        return None

    def _invocation_id(self, ctx: AgentContext) -> str | None:
        """Return invocation_id from session metadata, or ``None``."""
        if ctx.session is not None:
            inv = str(ctx.session.metadata.get("invocation_id", ""))
            if inv:
                return inv
        return None

    def _build_base_attrs(self, ctx: AgentContext, operation_name: str) -> dict[str, object]:
        """Build the common attribute set carried on every span.

        Includes agent name, operation name, conversation ID, Langfuse
        session/user IDs, trace name, provider name, and invocation ID.
        """
        attrs: dict[str, object] = {
            GenAiAttr.AGENT_NAME: self._agent_name(ctx),
            GenAiAttr.OPERATION_NAME: operation_name,
            GenAiAttr.CONVERSATION_ID: str(ctx.session),
            GenAiAttr.LANGFUSE_SESSION_ID: str(ctx.session),
        }
        user_id = self._user_id(ctx)
        if user_id is not None:
            attrs[GenAiAttr.LANGFUSE_USER_ID] = user_id
        turn_id = ctx.identity.turn_id if ctx.identity else None
        if turn_id is not None:
            attrs[GenAiAttr.LANGFUSE_TRACE_NAME] = f"{ctx.session}.{turn_id}"
        if self._provider_name is not None:
            attrs[GenAiAttr.PROVIDER_NAME] = self._provider_name
        inv = self._invocation_id(ctx)
        if inv is not None:
            attrs[GenAiAttr.INVOCATION_ID] = inv
        return attrs

    async def _last_user_input(self, ctx: AgentContext) -> str | None:
        """Extract the last user/agent message content from history.

        Iterates ``ctx.history`` in reverse, returning the string content
        of the first message with role ``"user"`` or ``"agent"``. Returns
        ``None`` if no such message exists or content is not a string.
        """
        try:
            all_msgs = await ctx.history.to_list()
        except Exception:
            return None
        for msg in reversed(list(all_msgs)[-20:]):
            if msg.role in ("user", "agent"):
                content = msg.content
                if isinstance(content, str):
                    return content
                return None
        return None

    async def _save_span(
        self,
        trace_id: str,
        span_id: str,
        parent_span_id: str | None,
        name: str,
        kind: str,
        start_time: float,
        end_time: float | None,
        attributes: dict[str, object],
        status: SpanStatus,
        ctx: AgentContext,
    ) -> None:
        """Construct a :class:`SpanModel` from individual fields and persist it.

        Creates the span, calls ``self._store.save_span(span)`` (which
        handles JSONL write + OTLP emission internally), and logs failures
        without raising. Returns early if no store is configured.
        """
        if self._store is None:
            return
        span = SpanModel(
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            name=name,
            kind=kind,
            start_time=start_time,
            end_time=end_time,
            attributes=attributes,
            status=status,
        )
        try:
            await self._store.save_span(span)
        except Exception:
            logger.warning("Trace hook failed to save span %s", name)
