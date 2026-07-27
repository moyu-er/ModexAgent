"""Internal agent communication service — pure router (ADR-0015 D5).

This service owns target validation, invocation_id semantics, session ID
construction, envelope building, and delivery. It NEVER creates agent
instances — subagent materialization is owned by ``AgentTemplate.materialize``
(invoked lazily by the poller-spawner in ``AgentPool``).
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.agent import AgentCommKind
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.messaging.broker import MessageBroker
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.bus import AgentMessageBus
from modex_agent.multi_agent.communication.result import AgentSendResult, format_send_ack
from modex_agent.multi_agent.communication.strategies.base import (
    SendDeps,
    SendRequest,
    SendStrategy,
    SendStrategyKind,
)
from modex_agent.multi_agent.communication.strategies.parent_reply import ParentReplyStrategy
from modex_agent.multi_agent.communication.strategies.peer_normal import PeerNormalStrategy
from modex_agent.multi_agent.communication.strategies.subagent_dispatch import (
    SubagentDispatchStrategy,
)
from modex_agent.multi_agent.communication.topology import TopologyPolicy
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.registry import AgentRegistry
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import CommunicationTarget, CommunicationTargetStore
from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver
from modex_agent.runtime.enums import TurnCustomKey
from modex_agent.trace.semconv import GenAiAttr, SpanKind, SpanName, SpanStatusCode
from modex_agent.trace.store import SpanModel, SpanStatus

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.pool import AgentPool

logger = logging.getLogger(__name__)


def _resolve_current_traceparent() -> str | None:
    """Resolve the current traceparent for cross-pool propagation.

    Does NOT generate a fresh traceparent — only forwards an existing
    context (OTel inject or ``os.environ``). Returns ``None`` when no
    trace context is active.
    """
    carrier: dict[str, str] = {}
    try:
        from opentelemetry import propagate  # type: ignore[import-not-found]
    except ImportError:
        pass
    else:
        propagate.inject(carrier)
    return carrier.get("traceparent") or os.environ.get("TRACEPARENT")


class _TracePropagatingPeerNormal(PeerNormalStrategy):
    """PeerNormalStrategy that stamps the current traceparent into the envelope.

    Overrides :meth:`build_envelope` to add the current ``traceparent``
    to the envelope's ``metadata`` so the receiving pool can continue the
    trace when it dispatches its own subprocesses.
    """

    def build_envelope(
        self,
        req: SendRequest,
        session: SessionInfo,
        invocation_id: str,
    ) -> AgentMessageEnvelope:
        envelope = super().build_envelope(req, session, invocation_id)
        traceparent = _resolve_current_traceparent()
        if traceparent:
            envelope.metadata["traceparent"] = traceparent
        return envelope


class AgentCommunicationService:
    """Internal service for inter-agent communication routing.

    Owns validation, invocation_id semantics, session ID building, envelope
    construction, and sync/async delivery selection. It is a pure router: it
    NEVER constructs agent instances.
    """

    def __init__(
        self,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        *,
        agent_bus: AgentMessageBus | None = None,
        session_factory: SessionIdFactory | None = None,
        session_registry: SessionRegistry | None = None,
        template_registry: AgentTemplateRegistry | None = None,
        pool: AgentPool | None = None,
        pool_name: str | None = None,
        project_dir: Path | None = None,
        target_store: CommunicationTargetStore | None = None,
        workspace_path_resolver: WorkspacePathResolver | None = None,
        trace_enabled: bool = True,
    ) -> None:
        self._source = source
        self._broker = broker
        self._registry = registry
        self._agent_bus = agent_bus
        self._session_factory = session_factory or SessionIdFactory()
        self._session_registry = session_registry
        self._template_registry = template_registry
        self._pool = pool
        self._pool_name = pool_name
        self._project_dir = project_dir
        self._target_store = target_store
        self._workspace_path_resolver = workspace_path_resolver

        deps = SendDeps(
            source=source,
            broker=broker,
            session_factory=self._session_factory,
            agent_bus=agent_bus,
            session_registry=session_registry,
            workspace_path_resolver=workspace_path_resolver,
            trace_enabled=trace_enabled,
        )
        self._strategies: dict[SendStrategyKind, SendStrategy] = {
            SendStrategyKind.SUBAGENT_DISPATCH: SubagentDispatchStrategy(deps),
            SendStrategyKind.PARENT_REPLY: ParentReplyStrategy(deps),
            SendStrategyKind.PEER_NORMAL: _TracePropagatingPeerNormal(deps),
        }

    async def send_async(
        self,
        *,
        target: CommunicationTarget,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> str:
        """Send asynchronously via inbox. Returns acknowledgement text."""
        result = await self._send(
            target=target,
            content=content,
            invocation_id=invocation_id,
            context=context,
        )
        return format_send_ack(result)

    async def _send(
        self,
        *,
        target: CommunicationTarget,
        content: str,
        invocation_id: str | None,
        context: AgentContext,
    ) -> AgentSendResult:
        """Core routing logic. Dispatches to one of three SendStrategy
        subclasses based on the target's routing kind."""
        err = TopologyPolicy.check(context.comm_kind, target, context)
        if err is not None:
            return AgentSendResult.with_error(
                target.name, target.kind, err
            )

        if target.bus_ref is not None:
            strategy = self._strategies[SendStrategyKind.PEER_NORMAL]
        elif target.kind == AgentCommKind.SUBAGENT:
            strategy = self._strategies[SendStrategyKind.SUBAGENT_DISPATCH]
        else:
            strategy = self._strategies[SendStrategyKind.PARENT_REPLY]

        await self._emit_handoff_span(context, target)

        req = SendRequest(
            target=target,
            content=content,
            invocation_id=invocation_id,
            context=context,
        )
        return await strategy.execute(req)

    async def _emit_handoff_span(
        self,
        context: AgentContext,
        target: CommunicationTarget,
    ) -> None:
        """Emit an ``agent.handoff`` span linking parent → child trace trees (G10).

        Fail-open: any error is logged and swallowed so tracing never blocks
        communication. The child's turn_id is unknown at send time, so
        ``child_turn_id`` is left ``None``; the child's ``invoke_agent`` span
        links back via the shared ``trace_id``.
        """
        try:
            runtime = context.runtime
            if runtime is None:
                return
            trace_store = runtime.services.trace_store
            if trace_store is None:
                return
            trace_id = runtime.state.custom.get(TurnCustomKey.TRACE_ID)
            if trace_id is None:
                return
            root_span_id = runtime.state.custom.get(TurnCustomKey.ROOT_SPAN_ID)
            message_type = (
                AgentMessageType.TASK_REQUEST
                if target.kind == AgentCommKind.SUBAGENT
                else AgentMessageType.AGENT_MESSAGE
            )
            now = time.time()
            span = SpanModel(
                trace_id=str(trace_id),
                span_id=uuid.uuid4().hex,
                parent_span_id=str(root_span_id) if root_span_id is not None else None,
                name=SpanName.AGENT_HANDOFF.value,
                kind=SpanKind.INTERNAL.value,
                start_time=now,
                end_time=now,
                attributes={
                    GenAiAttr.AGENT_NAME: self._source.name,
                    GenAiAttr.CONVERSATION_ID: str(context.session),
                    GenAiAttr.HANDOFF_TARGET_AGENT: target.name,
                    GenAiAttr.HANDOFF_TARGET_KIND: str(target.kind),
                    GenAiAttr.HANDOFF_MESSAGE_TYPE: str(message_type),
                    GenAiAttr.HANDOFF_PARENT_TURN_ID: (
                        context.identity.turn_id if context.identity is not None else None
                    ),
                    GenAiAttr.HANDOFF_CHILD_TURN_ID: None,
                },
                status=SpanStatus(code=SpanStatusCode.OK),
            )
            await trace_store.save_span(span)
        except Exception:
            logger.warning(
                "agent.handoff span emission failed for target=%s", target.name, exc_info=True
            )
