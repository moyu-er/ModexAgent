"""Strategy for SUBAGENT → parent NORMAL replies."""

from __future__ import annotations

from modex_agent.core.agent import AgentCommKind
from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.broker import AddressKind
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.strategies.base import SendRequest, SendStrategy
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.message_xml import build_agent_message


class ParentReplyStrategy(SendStrategy):
    """SUBAGENT → parent NORMAL reply.

    A subagent sends a reply to the NORMAL agent that dispatched its task.
    The reply reuses the parent's session (via ``parent_session_id``),
    uses ``AGENT_MESSAGE`` type. Orchestration is inherited from
    :meth:`SendStrategy.execute`.

    Note: in-pool NORMAL→NORMAL sends are routed here as a fallback
    because each pool has exactly one main agent, so this path is
    effectively unreachable for NORMAL senders in v1 (ADR-0019 §
    "Strategy #4 deferred").
    """

    def normalize_invocation_id(self, req: SendRequest) -> str | None:
        """NORMAL targets ignore invocation_id."""
        _ = req
        return None

    def build_session(self, req: SendRequest, invocation_id: str) -> SessionInfo:
        """Reuse parent session for subagent→parent, else mint a stable session."""
        _ = invocation_id  # NORMAL targets ignore invocation_id for session building
        parent_sid = req.context.session
        if (
            req.context.comm_kind == AgentCommKind.SUBAGENT
            and req.context.session.parent_session_id
        ):
            return SessionInfo.from_str(req.context.session.parent_session_id)
        return self._deps.session_factory.create(
            agent_name=req.target.name,
            parent_session_id=parent_sid,
            external_id=None,
        )

    def build_envelope(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentMessageEnvelope:
        """Build an AGENT_MESSAGE envelope."""
        _ = invocation_id  # derived from sender prefix internally, not from normalize
        effective_source = self._resolve_source(req)
        parent_sid = req.context.session

        envelope_invocation_id: str | None = None
        if req.context.comm_kind == AgentCommKind.SUBAGENT:
            envelope_invocation_id = parent_sid.session_id_prefix

        xml_content = build_agent_message(
            source=effective_source.name,
            invocation_id=envelope_invocation_id,
            content=req.content,
        )
        return AgentMessageEnvelope(
            payload={"content": xml_content, "message_type": AgentMessageType.AGENT_MESSAGE},
            source=effective_source,
            target=AgentAddress(kind=AddressKind.AGENT, name=req.target.name),
            message_type=AgentMessageType.AGENT_MESSAGE,
            session_id=str(parent_sid),
            agent_session_id=str(session),
            invocation_id=envelope_invocation_id,
        )

    def result_invocation_id(self, invocation_id: str) -> str | None:
        """NORMAL targets hide invocation_id from the sender's ack."""
        _ = invocation_id
        return None
