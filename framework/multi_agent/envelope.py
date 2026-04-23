from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from framework.messaging.broker import Address, BrokerMessage

if TYPE_CHECKING:
    from framework.multi_agent.address import AgentAddress


@dataclass
class AgentMessageEnvelope:
    """强制携带多 Agent 路由信息的通用消息信封。"""

    payload: dict[str, Any]
    source: AgentAddress
    target: AgentAddress | None = None
    topic: str | None = None
    message_type: str = "agent_message"
    conversation_id: str = ""
    agent_session_id: str = ""
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    in_reply_to: str | None = None
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    hop_count: int = field(default=0)

    def to_broker_message(self) -> BrokerMessage:
        """转换为 BrokerMessage，所有路由字段放入 headers。"""
        recipient = self.target or Address(kind="agent", name="")
        return BrokerMessage(
            payload=self.payload,
            sender=Address(kind=self.source.kind, name=self.source.name),
            recipient=recipient if self.target else None,
            topic=self.topic,
            headers={
                "conversation_id": self.conversation_id,
                "agent_session_id": self.agent_session_id,
                "message_id": self.message_id,
                "in_reply_to": self.in_reply_to or "",
                "message_type": self.message_type,
                "hop_count": str(self.hop_count),
                **self.metadata,
            },
            correlation_id=self.correlation_id,
            timestamp=self.timestamp,
        )

    @classmethod
    def from_broker_message(cls, msg: BrokerMessage) -> AgentMessageEnvelope | None:
        """从 BrokerMessage 还原，若缺少必要 headers 则返回 None。"""
        headers = msg.headers
        conversation_id = headers.get("conversation_id")
        agent_session_id = headers.get("agent_session_id")
        if not conversation_id or not agent_session_id:
            return None
        from framework.multi_agent.address import AgentAddress

        return cls(
            payload=msg.payload,
            source=AgentAddress(kind=msg.sender.kind, name=msg.sender.name),
            target=AgentAddress(kind=msg.recipient.kind, name=msg.recipient.name)
            if msg.recipient
            else None,
            topic=msg.topic,
            message_type=headers.get("message_type", "agent_message"),
            conversation_id=conversation_id,
            agent_session_id=agent_session_id,
            message_id=headers.get("message_id") or uuid.uuid4().hex,
            in_reply_to=headers.get("in_reply_to") or None,
            correlation_id=msg.correlation_id,
            timestamp=msg.timestamp,
            metadata={k: v for k, v in headers.items() if k not in {
                "conversation_id", "agent_session_id", "message_id",
                "in_reply_to", "message_type",
            }},
            hop_count=int(headers.get("hop_count", 0)),
        )
