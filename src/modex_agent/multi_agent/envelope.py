from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.messaging.broker import Address, AddressKind, BrokerMessage
from modex_agent.multi_agent.message_type import AgentMessageType

if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.core.types import InputMessage
    from modex_agent.multi_agent.address import AgentAddress


# Routing headers serialized into the broker message. Single source of truth:
# ``to_broker_message`` emits exactly these (plus invocation_id when present),
# and ``from_broker_message`` excludes them when rebuilding free-form metadata.
_ROUTING_HEADERS: frozenset[str] = frozenset(
    {
        "session_id",
        "agent_session_id",
        "parent_session_id",
        "message_id",
        "in_reply_to",
        "message_type",
        "invocation_id",
    }
)


@dataclass
class AgentMessageEnvelope:
    """强制携带多 Agent 路由信息的通用消息信封。

    Routing is driven by ``agent_session_id`` (the full ``SessionInfo`` string).
    ``invocation_id`` carries the source subagent's snowflake for trace
    correlation only — it does NOT participate in routing decisions.
    """

    payload: dict[str, Any]
    source: AgentAddress
    target: AgentAddress | None = None
    topic: str | None = None
    message_type: str = AgentMessageType.AGENT_MESSAGE
    session_id: str = ""
    agent_session_id: str = ""
    parent_session_id: str | None = None
    """Authoritative parent link for a subagent task dispatch.

    Set by the dispatching parent at send time (``_send`` SUBAGENT branch) and
    read by ``dispatch_envelope`` to stamp ``ctx.session.parent_session_id``.
    Carrying the parent in the message — instead of recovering it from a
    workspace-partitioned session store — is what makes subagent messaging
    independent of which workspace is active.
    """
    invocation_id: str | None = None
    """Source subagent's snowflake, for trace correlation only."""
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    in_reply_to: str | None = None
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_broker_message(self) -> BrokerMessage:
        """转换为 BrokerMessage，所有路由字段放入 headers。"""
        recipient = self.target or Address(kind=AddressKind.AGENT, name="")
        headers: dict[str, str] = {
            "session_id": self.session_id,
            "agent_session_id": self.agent_session_id,
            "message_id": self.message_id,
            "in_reply_to": self.in_reply_to or "",
            "message_type": self.message_type,
            **{k: str(v) for k, v in self.metadata.items()},
        }
        if self.invocation_id is not None:
            headers["invocation_id"] = self.invocation_id
        if self.parent_session_id is not None:
            headers["parent_session_id"] = self.parent_session_id
        return BrokerMessage(
            payload=self.payload,
            sender=Address(kind=self.source.kind, name=self.source.name),
            recipient=recipient if self.target else None,
            topic=self.topic,
            headers=headers,
            correlation_id=self.correlation_id,
            timestamp=self.timestamp,
        )

    @classmethod
    def from_broker_message(cls, msg: BrokerMessage) -> AgentMessageEnvelope | None:
        """从 BrokerMessage 还原，若缺少必要 headers 则返回 None。

        session_id/agent_session_id may be empty strings (legacy).
        Only reject when they are None (not present in headers at all).
        """
        headers = msg.headers
        session_id = headers.get("session_id")
        agent_session_id = headers.get("agent_session_id")
        if session_id is None or agent_session_id is None:
            return None
        from modex_agent.multi_agent.address import AgentAddress

        envelope_invocation_id = headers.get("invocation_id") or None
        envelope_parent_session_id = headers.get("parent_session_id") or None

        return cls(
            payload=msg.payload,
            source=AgentAddress(kind=msg.sender.kind, name=msg.sender.name),
            target=AgentAddress(kind=msg.recipient.kind, name=msg.recipient.name)
            if msg.recipient
            else None,
            topic=msg.topic,
            message_type=headers.get("message_type", AgentMessageType.AGENT_MESSAGE),
            session_id=session_id,
            agent_session_id=agent_session_id,
            parent_session_id=envelope_parent_session_id,
            invocation_id=envelope_invocation_id,
            message_id=headers.get("message_id") or uuid.uuid4().hex,
            in_reply_to=headers.get("in_reply_to") or None,
            correlation_id=msg.correlation_id,
            timestamp=msg.timestamp,
            metadata={k: v for k, v in headers.items() if k not in _ROUTING_HEADERS},
        )

    def to_input_metadata(self) -> dict[str, Any]:
        """Routing metadata for ``InputMessage.metadata`` when dispatching this envelope.

        ``source_agent`` / ``receiver_agent`` are present only when the source
        is an agent (not channel/user). ``sender_agent`` is intentionally
        omitted — it duplicated ``source_agent`` in the legacy pool-side helper.

        Free-form ``InputMessage.metadata`` serialized into the payload by
        ``submit_input`` (``BrokerInputPayload``) is merged back beneath the
        authoritative routing and envelope metadata.
        """
        from modex_agent.messaging.broker_bridge import BrokerInputPayload

        payload = BrokerInputPayload.model_validate(self.payload)
        return self._to_input_metadata(payload.metadata)

    def _to_input_metadata(self, payload_metadata: dict[str, Any]) -> dict[str, Any]:
        """Merge validated payload metadata with authoritative routing fields."""

        source_name = self.source.name if self.source else None
        target_name = self.target.name if self.target else None
        is_agent_source = bool(self.source and self.source.kind == AddressKind.AGENT)
        return {
            **payload_metadata,
            "session_id": self.agent_session_id,
            "agent_session_id": self.agent_session_id,
            "message_type": self.message_type,
            "invocation_id": self.invocation_id,
            "source_agent": source_name if is_agent_source else None,
            "receiver_agent": target_name if is_agent_source else None,
            **self.metadata,
        }

    def to_input_message(
        self,
        *,
        session: SessionInfo,
    ) -> InputMessage:
        """Reconstruct the :class:`InputMessage` dispatched to a pipeline.

        ``session`` must already carry ``parent_session_id`` (stamped by
        ``dispatch_envelope`` before this call).
        """
        from modex_agent.core.types import InputMessage
        from modex_agent.messaging.broker_bridge import BrokerInputPayload

        payload = BrokerInputPayload.model_validate(self.payload)
        return InputMessage(
            content=payload.content,
            session=session,
            metadata=self._to_input_metadata(payload.metadata),
            content_format=payload.content_format,
            truncatable_paths=payload.truncatable_paths,
            approval_decision=payload.to_approval_decision(),
            attachments_resolved=payload.attachments_resolved,
            workspace=Path(payload.workspace) if payload.workspace is not None else None,
        )
