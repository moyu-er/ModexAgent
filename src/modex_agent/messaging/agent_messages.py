"""Agent-message vocabulary — addresses, envelopes, message kinds, routing.

Sank into the messaging package (W3b, formerly
``multi_agent/{address,message_type,envelope,router}.py`` vocabulary): the
pipeline's context assembly (level 5) and the multi-agent runtime (level 6)
both speak this inter-agent message vocabulary, so it lives with the rest
of the message vocabulary in messaging (level 0). The mesh routing
implementation (``DefaultMeshRouter``) stays in
:mod:`modex_agent.multi_agent.router`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import Field

from modex_agent.core.session_id import SessionInfo
from modex_agent.messaging.broker import Address, AddressKind, BrokerMessage
from modex_agent.messaging.models import InputMessage

__all__ = [
    "AgentAddress",
    "AgentMessageEnvelope",
    "AgentMessageRouter",
    "AgentMessageType",
    "RouteResult",
]


class AgentMessageType(StrEnum):
    """The ``message_type`` of an ``AgentMessageEnvelope`` / inbox record."""

    #: Normal→subagent: start or continue a task. Always starts a fresh
    #: between-turn for the target subagent session.
    TASK_REQUEST = "task_request"

    #: Generic inter-agent message. Fold-in eligible (folded into a running
    #: turn as ``role=AGENT`` history when one is active).
    AGENT_MESSAGE = "agent_message"

    #: Subagent→parent *reply* emitted by ``SubagentAutoSendHook``.
    #: Fold-eligible: a busy parent agent mid-turn
    #: pulls it via ``InboxFlushHook`` so it sees the deliverable promptly; an
    #: idle parent receives it as a fresh between-turn via the poller.
    #: Distinct from ``SUBAGENT_RESULT``, which is retained only as a reserved
    #: legacy label for old persisted records.
    AGENT_RESULT = "agent_result"

    #: Reserved legacy subagent-result label with no current producers. Kept so
    #: old on-disk inbox records still parse; fold-in eligible. Do NOT conflate
    #: with ``AGENT_RESULT`` (see above).
    SUBAGENT_RESULT = "subagent_result"

    #: Human DM / WebUI / approval decision entering via ``pool.submit_input``.
    #: Never folded mid-turn — it is a new user input and must start its own
    #: between-turn (spec P6).
    EXTERNAL_INPUT = "external_input"

    @classmethod
    def fold_eligible(cls) -> frozenset[AgentMessageType]:
        """Message kinds the fold-in hook pulls mid-turn (``only_types``).

        Every inter-agent kind folds — including ``AGENT_RESULT`` (a subagent
        reply), so a busy parent agent mid-turn sees the deliverable promptly
        instead of only after its turn ends. ``EXTERNAL_INPUT`` is the sole
        exclusion: a human DM is a new user input and must start its own
        between-turn (spec P6).
        """
        return frozenset(m for m in cls if m != cls.EXTERNAL_INPUT)


class AgentAddress(Address):
    """Agent-specific address with role and capability metadata."""

    kind: AddressKind = AddressKind.AGENT
    name: str = ""
    role: str | None = None
    capabilities: list[str] = Field(default_factory=list)

    def __str__(self) -> str:
        base = f"{self.kind}:{self.name}"
        if self.role:
            base = f"{base}@{self.role}"
        if self.capabilities:
            caps = ",".join(self.capabilities)
            base = f"{base}[{caps}]"
        return base

    def __hash__(self) -> int:
        # Routing matches on kind+name only; role/capabilities are metadata and do not affect delivery
        return hash((self.kind, self.name))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Address):
            return NotImplemented
        return self.kind == other.kind and self.name == other.name


if TYPE_CHECKING:
    from modex_agent.core.session_id import SessionInfo
    from modex_agent.messaging.agent_messages import AgentAddress
    from modex_agent.messaging.models import InputMessage
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
    """Generic message envelope that mandatorily carries multi-agent routing info.

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
    message_id: str = field(default_factory=lambda: uuid4().hex)
    in_reply_to: str | None = None
    correlation_id: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_broker_message(self) -> BrokerMessage:
        """Convert to a BrokerMessage, with all routing fields placed into headers."""
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
        """Restore from a BrokerMessage; return None when required headers are missing.

        session_id/agent_session_id may be empty strings (legacy).
        Only reject when they are None (not present in headers at all).
        """
        headers = msg.headers
        session_id = headers.get("session_id")
        agent_session_id = headers.get("agent_session_id")
        if session_id is None or agent_session_id is None:
            return None
        from modex_agent.messaging.agent_messages import AgentAddress

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
            message_id=headers.get("message_id") or uuid4().hex,
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
        from modex_agent.messaging import BrokerInputPayload

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
        from modex_agent.messaging import BrokerInputPayload
        from modex_agent.messaging.models import InputMessage

        payload = BrokerInputPayload.model_validate(self.payload)
        return InputMessage(
            content=payload.content,
            session=session,
            metadata=self._to_input_metadata(payload.metadata),
            content_format=payload.content_format,
            truncatable_paths=payload.truncatable_paths,
            approval_decision=payload.approval_decision,
            attachments_resolved=payload.attachments_resolved,
            workspace=Path(payload.workspace) if payload.workspace is not None else None,
        )


@dataclass
class RouteResult:
    """Result of routing an input message to an agent-owned session."""

    session: SessionInfo
    envelope_metadata: dict[str, Any] | None = None
    is_envelope: bool = False


class AgentMessageRouter(ABC):
    """Decides the agent-owned session for an incoming message."""

    @abstractmethod
    def route(
        self,
        input_msg: InputMessage,
    ) -> RouteResult:
        """Route an input message and return the complete agent session id."""
        ...
