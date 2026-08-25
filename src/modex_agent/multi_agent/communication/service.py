"""Internal agent communication service — pure router (ADR-0015 D5).

This service owns target validation, invocation_id semantics, session ID
construction, envelope building, and delivery. It NEVER creates agent
instances — subagent materialization is owned by ``AgentTemplate.materialize``
(invoked lazily by the poller-spawner in ``AgentPool``).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.core.agent import AgentCommKind
from modex_agent.core.session_id import SessionIdFactory
from modex_agent.core.session_registry import SessionRegistry
from modex_agent.multi_agent.address import AgentAddress
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
from modex_agent.multi_agent.registry import AgentRegistry
from modex_agent.multi_agent.template_registry import AgentTemplateRegistry
from modex_agent.multi_agent.tools import CommunicationTarget, CommunicationTargetStore

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.multi_agent.pool import AgentPool
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.workspace.resources import WorkspaceManager
    from modex_agent.workspace.scope_path import ScopePath

logger = logging.getLogger(__name__)


def _resolve_current_traceparent() -> str | None:
    """Resolve the current traceparent for cross-pool propagation.

    Does NOT generate a fresh traceparent — only forwards an existing
    context (OTel inject or ``os.environ``). Returns ``None`` when no
    trace context is active.
    """
    carrier: dict[str, str] = {}
    try:
        from opentelemetry import propagate
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
        registry: AgentRegistry,
        *,
        tree: SessionTreeManager,
        session_factory: SessionIdFactory | None = None,
        session_registry: SessionRegistry | None = None,
        template_registry: AgentTemplateRegistry | None = None,
        pool: AgentPool | None = None,
        pool_name: str | None = None,
        project_dir: Path | None = None,
        target_store: CommunicationTargetStore | None = None,
        scope_path: ScopePath | None = None,
        workspace_manager: WorkspaceManager | None = None,
        trace_enabled: bool = True,
    ) -> None:
        self._source = source
        self._registry = registry
        self._tree = tree
        self._session_factory = session_factory or SessionIdFactory()
        self._session_registry = session_registry
        self._template_registry = template_registry
        self._pool = pool
        self._pool_name = pool_name
        self._project_dir = project_dir
        self._target_store = target_store
        deps = SendDeps(
            source=source,
            session_factory=self._session_factory,
            tree=tree,
            session_registry=session_registry,
            scope_path=scope_path,
            workspace_manager=workspace_manager,
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
        err = TopologyPolicy.check(
            context.comm_kind,
            target,
            context,
            declared_children=self._declared_children(),
        )
        if err is not None:
            return AgentSendResult.with_error(target.name, target.kind, err)

        if target.tree_ref is not None:
            strategy = self._strategies[SendStrategyKind.PEER_NORMAL]
        elif target.kind == AgentCommKind.SUBAGENT:
            strategy = self._strategies[SendStrategyKind.SUBAGENT_DISPATCH]
        else:
            strategy = self._strategies[SendStrategyKind.PARENT_REPLY]

        req = SendRequest(
            target=target,
            content=content,
            invocation_id=invocation_id,
            context=context,
        )
        return await strategy.execute(req)

    def _declared_children(self) -> frozenset[str]:
        """The sender's declared direct children (SUBAGENT entries of the
        sender's per-agent target store — SPEC §5.2's derived-children
        carrier). Empty for leaf agents and services without a store."""
        if self._target_store is None:
            return frozenset()
        return self._target_store.subagent_names()
