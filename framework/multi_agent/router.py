from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from framework.core.types import InputMessage
from framework.multi_agent.session_id import DefaultSessionIdStrategy


@dataclass
class RouteResult:
    """Result of routing an input message to an agent-owned session."""

    conversation_id: str
    agent_session_id: str
    agent_name: str
    prompt_modifier: str | None = None
    envelope_metadata: dict[str, Any] | None = None
    is_envelope: bool = False


class AgentMessageRouter(ABC):
    """Decides the agent-owned session for an incoming message."""

    @abstractmethod
    def route(
        self,
        input_msg: InputMessage,
        default_agent_name: str = "main",
    ) -> RouteResult:
        """Route an input message and return the complete agent session id."""
        ...


class DefaultMeshRouter(AgentMessageRouter):
    """Default router for receiver-owned agent sessions.

    The router, not the pipeline, constructs fallback agent session IDs. The
    pipeline then uses the returned session ID for locking and memory scope.
    """

    def route(
        self,
        input_msg: InputMessage,
        default_agent_name: str = "main",
    ) -> RouteResult:
        metadata = input_msg.metadata or {}
        strategy = DefaultSessionIdStrategy()
        conversation_id = str(metadata.get("conversation_id") or input_msg.session_id)
        agent_session_id = metadata.get("agent_session_id")
        agent_name = default_agent_name

        if agent_session_id:
            agent_session_id = str(agent_session_id)
            parts = strategy.parse(agent_session_id)
            if parts.agent_name is not None:
                conversation_id = str(metadata.get("conversation_id") or parts.conversation_id)
                agent_name = parts.agent_name
        else:
            agent_session_id = strategy.format(
                conversation_id=conversation_id,
                agent_name=default_agent_name,
            )

        prompt_modifier = None
        message_type = metadata.get("message_type", "agent_message")
        is_envelope = message_type in ("agent_message", "subagent_result", "rpc_request")

        if message_type == "subagent_result" and metadata.get("source_agent"):
            prompt_modifier = f"[Subagent {metadata['source_agent']} result]\n\n"

        return RouteResult(
            conversation_id=conversation_id,
            agent_session_id=agent_session_id,
            agent_name=agent_name,
            prompt_modifier=prompt_modifier,
            envelope_metadata=dict(metadata),
            is_envelope=is_envelope,
        )
