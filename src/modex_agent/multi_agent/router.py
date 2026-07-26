from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage
from modex_agent.multi_agent.message_type import AgentMessageType

if TYPE_CHECKING:
    from modex_agent.core.session_registry import SessionRegistry


@dataclass
class RouteResult:
    """Result of routing an input message to an agent-owned session."""

    session: SessionInfo
    prompt_modifier: str | None = None
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


class DefaultMeshRouter(AgentMessageRouter):
    """Default router that trusts ``input_msg.session`` as the authoritative identity.

    The pipeline uses ``route_result.session`` for locking and memory scope.
    Metadata is inspected only for envelope classification and prompt modifiers;
    the session identity is never parsed from metadata strings.
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

        prompt_modifier = None
        message_type = metadata.get("message_type", AgentMessageType.AGENT_MESSAGE)
        is_envelope = message_type in (
            AgentMessageType.AGENT_MESSAGE,
            AgentMessageType.SUBAGENT_RESULT,
            "rpc_request",
        )

        if message_type == AgentMessageType.SUBAGENT_RESULT and metadata.get("source_agent"):
            prompt_modifier = f"[Subagent {metadata['source_agent']} result]\n\n"

        return RouteResult(
            session=session,
            prompt_modifier=prompt_modifier,
            envelope_metadata=dict(metadata),
            is_envelope=is_envelope,
        )
