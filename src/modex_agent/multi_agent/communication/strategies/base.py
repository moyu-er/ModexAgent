"""SendStrategy ABC and dependency bundles for communication strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.comm_kind import AgentCommKind
from modex_agent.multi_agent.communication.result import AgentSendResult
from modex_agent.multi_agent.envelope import AgentMessageEnvelope

if TYPE_CHECKING:
    from modex_agent.core.agent import AgentContext
    from modex_agent.core.session_id import SessionIdFactory, SessionInfo
    from modex_agent.core.session_registry import SessionRegistry
    from modex_agent.multi_agent.session_tree.manager import SessionTreeManager
    from modex_agent.multi_agent.tools import CommunicationTarget
    from modex_agent.multi_agent.workspace_paths import WorkspacePathResolver


class SendStrategyKind(StrEnum):
    """Identifies a send strategy topology (ADR-0019).

    Used as the dispatch key in ``AgentCommunicationService._strategies``
    instead of raw strings, per type-safety rule #1.
    """

    SUBAGENT_DISPATCH = "subagent_dispatch"
    PARENT_REPLY = "parent_reply"
    PEER_NORMAL = "peer_normal"


@dataclass(frozen=True)
class SendDeps:
    """Dependencies shared by all send strategies."""

    source: AgentAddress
    session_factory: SessionIdFactory
    tree: SessionTreeManager
    session_registry: SessionRegistry | None = None
    workspace_path_resolver: WorkspacePathResolver | None = None
    trace_enabled: bool = True


@dataclass(frozen=True)
class SendRequest:
    """A single send request — the input to a strategy."""

    target: CommunicationTarget
    content: str
    invocation_id: str | None
    context: AgentContext


class SendStrategy(ABC):
    """Handles one send to one target topology.

    The orchestration sequence (``execute``) is fixed in the base class;
    concrete strategies override the individual hooks (``normalize_invocation_id``,
    ``build_session``, ``build_envelope``, ``deliver``) and
    the result-shaping helpers (``should_register_session``, ``build_result``).
    """

    def __init__(self, deps: SendDeps) -> None:
        self._deps = deps

    # --- orchestration template (concrete, final-shaped) ------------------

    async def execute(self, req: SendRequest) -> AgentSendResult:
        """Run the full send sequence: normalize → session → envelope → deliver."""
        invocation_id = self.normalize_invocation_id(req) or ""
        session = self.build_session(req, invocation_id)
        if self.should_register_session() and self._deps.session_registry is not None:
            await self._deps.session_registry.register(session)
        envelope = self.build_envelope(req, session, invocation_id)
        if req.context.graph_instance_id is not None:
            envelope.metadata["graph_instance_id"] = req.context.graph_instance_id
        deliver_err = await self.deliver(envelope, req.target)
        if deliver_err is not None:
            return AgentSendResult.with_error(
                req.target.name,
                req.target.kind,
                deliver_err,
                session_id=str(session),
                invocation_id=self.result_invocation_id(invocation_id),
            )
        return self.build_result(req, session, invocation_id)

    # --- hooks (each strategy overrides what it needs) --------------------

    @abstractmethod
    def normalize_invocation_id(self, req: SendRequest) -> str | None: ...

    @abstractmethod
    def build_session(self, req: SendRequest, invocation_id: str) -> SessionInfo: ...

    @abstractmethod
    def build_envelope(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentMessageEnvelope: ...

    async def deliver(self, env: AgentMessageEnvelope, target: CommunicationTarget) -> str | None:
        """Default delivery: tree.deliver (converged — single path, no fallback).

        Subclasses with a different delivery target (e.g. peer-pool tree)
        override this.
        """
        _ = target
        return await self._deliver(env)

    # --- result-shaping hooks (default = simple; strategies override) -----

    def should_register_session(self) -> bool:
        """Whether to register the new session in the local SessionRegistry.

        Default: False. ``SubagentDispatchStrategy`` overrides to True
        (subagent sessions are owned by the sender's pool).
        Peer-normal sessions are registered by the *receiver's* poller, not
        the sender — so the default is False.
        """
        return False

    def result_invocation_id(self, invocation_id: str) -> str | None:
        """What invocation_id to surface in the AgentSendResult.

        Default: pass through. ``ParentReply`` and ``PeerNormal`` override
        to return None (NORMAL targets hide invocation_id from the sender).
        """
        return invocation_id

    def build_result(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentSendResult:
        """Build the successful-send AgentSendResult.

        Default: no peer flag, no trace/output paths, invocation_id passed
        through. Strategies override to add their specifics.
        """
        return AgentSendResult(
            target_agent=req.target.name,
            target_kind=req.target.kind,
            session_id=str(session),
            invocation_id=self.result_invocation_id(invocation_id),
            created_new_task=False,
        )

    # --- shared helpers ---------------------------------------------------

    def _envelope_payload(self, content: str, message_type: str, req: SendRequest) -> dict[str, Any]:
        """Build the envelope payload dict, including workspace when bound."""
        payload: dict[str, Any] = {"content": content, "message_type": message_type}
        if req.context.workspace is not None:
            payload["workspace"] = str(req.context.workspace)
        return payload

    def _resolve_source(self, req: SendRequest) -> AgentAddress:
        """Resolve effective source address from context, fallback to default."""
        if req.context.session.agent_name:
            return AgentAddress(name=req.context.session.agent_name)
        return self._deps.source

    async def _deliver(self, envelope: AgentMessageEnvelope) -> str | None:
        """Single delivery path: tree.deliver (converged — no fallback)."""
        await self._deps.tree.deliver(envelope.agent_session_id, envelope)
        return None

    def _subagent_runtime_dir(self, target_kind: AgentCommKind | None) -> Path | None:
        """Resolved runtime_dir for SUBAGENT targets, else None."""
        if target_kind != AgentCommKind.SUBAGENT:
            return None
        if self._deps.workspace_path_resolver is None:
            return None
        return self._deps.workspace_path_resolver.runtime_dir()

    def _subagent_trace_dir(
        self, target_kind: AgentCommKind | None, session_id: str
    ) -> Path | None:
        """Compute subagent execution-trace dir for the ack text."""
        if not self._deps.trace_enabled:
            return None
        runtime_dir = self._subagent_runtime_dir(target_kind)
        if runtime_dir is None:
            return None
        return runtime_dir / "trace" / session_id
