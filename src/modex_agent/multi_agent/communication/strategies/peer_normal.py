"""Strategy for NORMAL → NORMAL cross-pool peer sends."""

from __future__ import annotations

from modex_agent.core.agent import AgentImplementation
from modex_agent.core.constants import ExecutionStrategyKind
from modex_agent.core.session_id import SessionInfo
from modex_agent.multi_agent.address import AgentAddress
from modex_agent.multi_agent.communication.result import AgentSendResult
from modex_agent.multi_agent.communication.strategies.base import SendRequest, SendStrategy
from modex_agent.multi_agent.envelope import AgentMessageEnvelope
from modex_agent.multi_agent.message_type import AgentMessageType
from modex_agent.multi_agent.message_xml import build_peer_agent_message
from modex_agent.multi_agent.tools import CommunicationTarget


class PeerNormalStrategy(SendStrategy):
    """Send from a NORMAL agent to a NORMAL agent in another pool.

    Reuses the sender's session prefix so the receiver sees a stable,
    sender-scoped conversation id. The ``invocation_id`` is hidden from
    the sender's ack and from the receiver's XML. Delivery targets the
    peer pool's bus via ``target.bus_ref``. Orchestration is inherited
    from :meth:`SendStrategy.execute`.
    """

    def normalize_invocation_id(self, req: SendRequest) -> str | None:
        """Return the sender's session prefix for internal bookkeeping."""
        return req.context.session.session_id_prefix

    def build_session(self, req: SendRequest, invocation_id: str) -> SessionInfo:
        """Reuse sender's prefix; peer agents are equals, so no parent."""
        _ = invocation_id  # prefix is read from context, not from the normalized id
        sender_prefix = req.context.session.session_id_prefix
        return self._deps.session_factory.create_with_prefix(
            agent_name=req.target.name,
            prefix=sender_prefix,
        )

    def build_envelope(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentMessageEnvelope:
        """Build an AGENT_MESSAGE envelope with hidden invocation_id."""
        _ = invocation_id  # envelope uses sender prefix directly
        effective_source = self._resolve_source(req)
        sender_sid = req.context.session
        envelope_invocation_id = self.normalize_invocation_id(req)

        xml_content = build_peer_agent_message(
            source=effective_source.name,
            content=req.content,
            receiver_implementation=(
                AgentImplementation.EXTERNAL
                if req.target.execution_strategy == ExecutionStrategyKind.EXTERNAL
                else AgentImplementation.NATIVE
            ),
        )
        return AgentMessageEnvelope(
            payload={"content": xml_content, "message_type": AgentMessageType.AGENT_MESSAGE},
            source=effective_source,
            target=AgentAddress(name=req.target.name),
            message_type=AgentMessageType.AGENT_MESSAGE,
            session_id=str(sender_sid),
            agent_session_id=str(session),
            invocation_id=envelope_invocation_id,
        )

    async def deliver(self, env: AgentMessageEnvelope, target: CommunicationTarget) -> str | None:
        """Deliver to the target's bus, falling back to the local bus."""
        bus = target.bus_ref or self._deps.agent_bus
        if bus is None:
            return "No bus available for delivery"
        await bus.send(env.agent_session_id, env)
        return None

    def result_invocation_id(self, invocation_id: str) -> str | None:
        """Peer sends hide invocation_id from the sender's ack."""
        _ = invocation_id
        return None

    def build_result(
        self, req: SendRequest, session: SessionInfo, invocation_id: str
    ) -> AgentSendResult:
        """Mark the result as a peer send (no trace/output paths)."""
        return AgentSendResult(
            target_agent=req.target.name,
            target_kind=req.target.kind,
            session_id=str(session),
            invocation_id=None,
            created_new_task=False,
            is_peer_send=True,
        )
