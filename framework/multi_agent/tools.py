"""Multi-agent communication tools — thin wrappers around AgentCommunicationService.

The LLM sees only two tools:
- SendToAgentTool: synchronous broker/wakeup delivery.
- SendToAgentAsyncTool: inbox-based async delivery.

The old SendMessageTool, SendMessageAsyncTool, and DispatchTaskTool are removed.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from framework.core.tool_manager import Tool, ToolConfig
from framework.multi_agent.comm_kind import AgentCommKind

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.messaging.broker import MessageBroker
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.bus import AgentMessageBus
    from framework.multi_agent.comm_tracker import CommunicationTracker
    from framework.multi_agent.communication import AgentCommunicationService
    from framework.multi_agent.registry import AgentRegistry
    from framework.multi_agent.session_id import DefaultSessionIdStrategy

logger = logging.getLogger(__name__)

_UUID_PARAM = {
    "type": ["string", "null"],
    "description": (
        "Routing selector. Use null for normal-agent delivery. "
        "Use an empty string to start a new subagent task. "
        "Use a concrete uuid to continue an existing subagent task."
    ),
}

_COMMON_PARAMS: dict[str, dict[str, Any]] = {
    "target_agent": {
        "type": "string",
        "description": "Name of the target agent.",
    },
    "content": {
        "type": "string",
        "description": "Message content.",
    },
    "uuid": _UUID_PARAM,
}


def _build_dynamic_description(
    service: AgentCommunicationService,
    base_description: str,
) -> str:
    """Append available targets and uuid guidance to the base description."""
    targets_desc = service.build_targets_description()
    return f"{base_description}\n\n{targets_desc}"


class SendToAgentTool(Tool):
    """Synchronous send-to-agent tool using broker wakeup delivery.

    Registered by callers that want immediate target execution.
    ``bot_project`` does NOT register this tool — it uses async only.
    """

    def __init__(
        self,
        *,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        service: AgentCommunicationService,
        comm_tracker: CommunicationTracker | None = None,
    ) -> None:
        self._source = source
        self._broker = broker
        self._registry = registry
        self._service = service
        self._comm_tracker = comm_tracker
        super().__init__(
            name="send_to_agent",
            description="Send a message to another agent and trigger immediate processing.",
            parameters={
                "type": "object",
                "properties": _COMMON_PARAMS,
                "required": ["target_agent", "content", "uuid"],
            },
            config=ToolConfig(),
        )

    def get_dynamic_schema(self, caller_context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": _build_dynamic_description(self._service, self.description),
                "parameters": self.parameters,
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        target_agent = str(kwargs.get("target_agent", ""))
        content = str(kwargs.get("content", ""))
        uuid_value = kwargs.get("uuid")
        uuid: str | None = None if uuid_value is None else str(uuid_value)

        context = self._get_context()
        if context is None:
            return "Error: no agent context available"
        return await self._service.send_sync(
            target_agent=target_agent, content=content, uuid=uuid, context=context,
        )

    @staticmethod
    def _get_context() -> AgentContext | None:
        import contextvars
        from framework.core.agent import current_agent_context
        return current_agent_context.get(None)


class SendToAgentAsyncTool(Tool):
    """Asynchronous send-to-agent tool using inbox delivery.

    This is the primary multi-agent communication tool registered by ``bot_project``.
    """

    def __init__(
        self,
        *,
        source: AgentAddress,
        broker: MessageBroker,
        registry: AgentRegistry,
        agent_bus: AgentMessageBus,
        service: AgentCommunicationService,
        comm_tracker: CommunicationTracker | None = None,
        wakeup_timeout: float = 1.0,
    ) -> None:
        self._source = source
        self._broker = broker
        self._registry = registry
        self._agent_bus = agent_bus
        self._service = service
        self._comm_tracker = comm_tracker
        self._wakeup_timeout = wakeup_timeout
        super().__init__(
            name="send_to_agent_async",
            description="Send a message to another agent's inbox for asynchronous processing.",
            parameters={
                "type": "object",
                "properties": _COMMON_PARAMS,
                "required": ["target_agent", "content", "uuid"],
            },
            config=ToolConfig(),
        )

    def get_dynamic_schema(self, caller_context: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": _build_dynamic_description(self._service, self.description),
                "parameters": self.parameters,
            },
        }

    async def execute(self, **kwargs: Any) -> str:
        target_agent = str(kwargs.get("target_agent", ""))
        content = str(kwargs.get("content", ""))
        uuid_value = kwargs.get("uuid")
        uuid: str | None = None if uuid_value is None else str(uuid_value)

        context = self._get_context()
        if context is None:
            return "Error: no agent context available"
        return await self._service.send_async(
            target_agent=target_agent, content=content, uuid=uuid, context=context,
        )

    @staticmethod
    def _get_context() -> AgentContext | None:
        import contextvars
        from framework.core.agent import current_agent_context
        return current_agent_context.get(None)
