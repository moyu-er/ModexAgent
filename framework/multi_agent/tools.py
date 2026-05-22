"""Multi-agent communication tools — thin wrappers around AgentCommunicationService.

The LLM sees three tools in the communication system:
- ListCommunicationTargetsTool: discovery — MUST be called first to see available
  targets, their kinds, and invocation_id requirements.
- SendToAgentTool: synchronous broker/wakeup delivery.
- SendToAgentAsyncTool: inbox-based async delivery.

The send tools require exact invocation_id values obtained from the listing tool.
They do NOT perform target discovery or permission validation — the listing tool
handles that.
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

logger = logging.getLogger(__name__)

_INVOCATION_ID_PARAM = {
    "type": ["string", "null"],
    "description": (
        "Routing selector. Use null for normal-agent delivery. "
        "Use an empty string to start a new subagent task. "
        "Use a concrete invocation_id to continue an existing subagent task."
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
    "invocation_id": _INVOCATION_ID_PARAM,
}


def _build_dynamic_description(
    service: AgentCommunicationService,
    base_description: str,
) -> str:
    """Append available targets and invocation_id guidance to the base description."""
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
            description=(
                "Send a message to another agent and trigger immediate processing. "
                "Call list_communication_targets FIRST to see available agents "
                "and their required invocation_id format."
            ),
            parameters={
                "type": "object",
                "properties": _COMMON_PARAMS,
                "required": ["target_agent", "content", "invocation_id"],
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
        invocation_id_value = kwargs.get("invocation_id")
        invocation_id: str | None = None if invocation_id_value is None else str(invocation_id_value)

        context = self._get_context()
        if context is None:
            return "Error: no agent context available"
        return await self._service.send_sync(
            target_agent=target_agent, content=content, invocation_id=invocation_id, context=context,
        )

    @staticmethod
    def _get_context() -> AgentContext | None:
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
            description=(
                "Send a message to another agent's inbox for asynchronous processing. "
                "Call list_communication_targets FIRST to see available agents "
                "and their required invocation_id format."
            ),
            parameters={
                "type": "object",
                "properties": _COMMON_PARAMS,
                "required": ["target_agent", "content", "invocation_id"],
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
        invocation_id_value = kwargs.get("invocation_id")
        invocation_id: str | None = None if invocation_id_value is None else str(invocation_id_value)

        context = self._get_context()
        if context is None:
            return "Error: no agent context available"
        return await self._service.send_async(
            target_agent=target_agent, content=content, invocation_id=invocation_id, context=context,
        )

    @staticmethod
    def _get_context() -> AgentContext | None:
        from framework.core.agent import current_agent_context
        return current_agent_context.get(None)


class ListCommunicationTargetsTool(Tool):
    """Discovery tool — lists agents the current agent can communicate with.

    **MUST be called first** before using send_to_agent / send_to_agent_async.
    The send tools require precise invocation_id values that depend on the
    target's kind. This tool provides:
    - Target names and descriptions
    - Kind (NORMAL or SUBAGENT) — determines invocation_id semantics
    - Required invocation_id format for each target
    """

    def __init__(
        self,
        *,
        self_address: AgentAddress,
        registry: AgentRegistry,
    ) -> None:
        self._self_address = self_address
        self._registry = registry
        super().__init__(
            name="list_communication_targets",
            description=(
                "List all agents available for communication. "
                "MUST be called BEFORE send_to_agent or send_to_agent_async "
                "to verify target existence, kind, and invocation_id requirements. "
                "Sending without calling this first may result in errors."
            ),
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            config=ToolConfig(),
        )

    async def execute(self, **kwargs: Any) -> str:
        _ = kwargs  # unused
        profiles = self._registry.list_profiles()
        current_name = self._self_address.name
        current_profile = next((p for p in profiles if p.name == current_name), None)

        targets = [p for p in profiles if p.name != current_name]
        if current_profile is not None and current_profile.comm_kind == AgentCommKind.SUBAGENT:
            targets = [p for p in targets if p.comm_kind == AgentCommKind.NORMAL]
        if not targets:
            return "No other agents are currently available for communication."

        lines = ["# Available Communication Targets", ""]
        lines.append("Call this tool FIRST before sending messages. The send tools")
        lines.append("require exact invocation_id values shown below for each target.")
        lines.append("")

        for p in targets:
            kind_label = p.comm_kind.value.upper()
            lines.append(f"## {p.name}")
            lines.append(f"  Kind: {kind_label}")

            if p.role_description:
                lines.append(f"  Description: {p.role_description}")

            if p.comm_kind == AgentCommKind.NORMAL:
                lines.append("  invocation_id: MUST be null")
                lines.append(f'    Example: send_to_agent_async(target_agent="{p.name}", content="...", invocation_id=null)')
            else:
                lines.append('  invocation_id: "" (new task) OR "<existing>" (continue task)')
                lines.append(f'    New task: send_to_agent_async(target_agent="{p.name}", content="...", invocation_id="")')
                lines.append(f'    Continue: send_to_agent_async(target_agent="{p.name}", content="...", invocation_id="<from previous reply>")')
            lines.append("")

        # Summary table
        lines.append("## Summary")
        lines.append("| Agent | Kind | invocation_id |")
        lines.append("|-------|------|---------------|")
        for p in targets:
            if p.comm_kind == AgentCommKind.NORMAL:
                lines.append(f"| {p.name} | NORMAL | null |")
            else:
                lines.append(f'| {p.name} | SUBAGENT | "" (new) or existing |')

        return "\n".join(lines)
