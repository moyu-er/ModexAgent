from __future__ import annotations

from typing import TYPE_CHECKING

from modex_agent.messaging.agent_messages import AgentMessageRouter, AgentMessageType, RouteResult
from modex_agent.messaging.models import InputMessage

if TYPE_CHECKING:
    from modex_agent.persistence.session_registry import SessionRegistry


class DefaultMeshRouter(AgentMessageRouter):
    """Default router that trusts ``input_msg.session`` as the authoritative identity.

    The pipeline uses ``route_result.session`` for locking and memory scope.
    Metadata is inspected only for envelope classification; the session identity
    is never parsed from metadata strings.
    """

    def __init__(
        self,
        session_registry: SessionRegistry | None = None,
    ) -> None:
        self._session_registry = session_registry

    def route(
        self,
        input_msg: InputMessage,
    ) -> RouteResult:
        metadata = input_msg.metadata or {}
        session = input_msg.session

        message_type = metadata.get("message_type", AgentMessageType.AGENT_MESSAGE)
        is_envelope = message_type in (
            AgentMessageType.AGENT_MESSAGE,
            AgentMessageType.SUBAGENT_RESULT,
            "rpc_request",
        )

        return RouteResult(
            session=session,
            envelope_metadata=dict(metadata),
            is_envelope=is_envelope,
        )
