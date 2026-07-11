"""Strategy-dispatched inter-agent communication package."""

from __future__ import annotations

from modex_agent.multi_agent.communication.result import AgentSendResult
from modex_agent.multi_agent.communication.service import AgentCommunicationService

__all__ = ["AgentCommunicationService", "AgentSendResult"]
