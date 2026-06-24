from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from modex_agent.messaging.broker import Address, BrokerMessage

if TYPE_CHECKING:
    from modex_agent.multi_agent.address import AgentAddress


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
    message_type: str = "agent_message"
    session_id: str = ""
    agent_session_id: str = ""
    invocation_id: str | None = None
    """Source subagent's snowflake, for trace correlation only."""
    message_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    in_reply_to: str | None = None
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    hop_count: int = field(default=0)

    def to_broker_message(self) -> BrokerMessage:
        """转换为 BrokerMessage，所有路由字段放入 headers。"""
        recipient = self.target or Address(kind="agent", name="")
        headers: dict[str, str] = {
            "session_id": self.session_id,
            "agent_session_id": self.agent_session_id,
            "message_id": self.message_id,
            "in_reply_to": self.in_reply_to or "",
            "message_type": self.message_type,
            "hop_count": str(self.hop_count),
            **{k: str(v) for k, v in self.metadata.items()},
        }
        if self.invocation_id is not None:
            headers["invocation_id"] = self.invocation_id
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

        return cls(
            payload=msg.payload,
            source=AgentAddress(kind=msg.sender.kind, name=msg.sender.name),
            target=AgentAddress(kind=msg.recipient.kind, name=msg.recipient.name)
            if msg.recipient
            else None,
            topic=msg.topic,
            message_type=headers.get("message_type", "agent_message"),
            session_id=session_id,
            agent_session_id=agent_session_id,
            invocation_id=envelope_invocation_id,
            message_id=headers.get("message_id") or uuid.uuid4().hex,
            in_reply_to=headers.get("in_reply_to") or None,
            correlation_id=msg.correlation_id,
            timestamp=msg.timestamp,
            metadata={
                k: v
                for k, v in headers.items()
                if k
                not in {
                    "session_id",
                    "agent_session_id",
                    "message_id",
                    "in_reply_to",
                    "message_type",
                    "invocation_id",
                }
            },
            hop_count=int(headers.get("hop_count", 0)),
        )
