"""Multi-agent communication tools — thin wrappers around AgentCommunicationService.

The LLM sees two tools in the communication system:
- ListCommunicationTargetsTool: discovery — MUST be called first to see available
  targets, their kinds, and invocation_id requirements.
- SendToAgentTool: inbox-based async delivery.

The send tool requires exact invocation_id values obtained from the listing tool.
It does NOT perform target discovery or permission validation — the listing tool
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
    from framework.multi_agent.template_registry import AgentTemplateRegistry

logger = logging.getLogger(__name__)

_INVOCATION_ID_PARAM = {
    "type": ["string", "null"],
    "description": (
        "Session identifier. Use null for a new task. "
        "Use a concrete invocation_id from a previous reply to continue an existing session."
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
            name="send_to_agent",
            description=(
                "Send a message to another agent asynchronously. "
                "Results arrive via inbox — this tool does NOT return the actual result directly. "
                "Use invocation_id=null for a new task, or pass a previous invocation_id to continue a session. "
                "Call list_communication_targets FIRST to see available targets."
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

    **MUST be called first** before using send_to_agent.
    The send tool requires precise invocation_id values that depend on the
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
        template_registry: AgentTemplateRegistry | None = None,
        pool_name: str | None = None,
    ) -> None:
        self._self_address = self_address
        self._registry = registry
        self._template_registry = template_registry
        self._pool_name = pool_name
        super().__init__(
            name="list_communication_targets",
            description=(
                "List all agents available for communication. "
                "MUST be called BEFORE send_to_agent "
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

        # Resolve current agent identity from context (not hardcoded constructor param)
        from framework.core.agent import current_agent_context
        ctx = current_agent_context.get(None)
        current_name = self._self_address.name
        current_comm_kind: AgentCommKind | None = None
        if ctx is not None and ctx.session_meta is not None:
            current_name = ctx.session_meta.agent_name
            current_comm_kind = ctx.session_meta.comm_kind

        profiles = self._registry.list_profiles()
        current_profile = next((p for p in profiles if p.name == current_name), None)
        if current_comm_kind is None and current_profile is not None:
            current_comm_kind = current_profile.comm_kind

        # Filter targets: exclude self only
        targets = [p for p in profiles if p.name != current_name]

        # Subagents can only see NORMAL targets
        if current_comm_kind == AgentCommKind.SUBAGENT:
            targets = [p for p in targets if p.comm_kind == AgentCommKind.NORMAL]

        # Check if templates exist (needed for both early return and summary)
        templates: list = []
        if self._template_registry is not None and self._pool_name is not None:
            templates = self._template_registry.list_templates(self._pool_name)

        if not targets and not templates:
            return "No other agents are currently available for communication."

        lines = ["# Available Communication Targets", ""]

        for p in targets:
            lines.append(f"## {p.name}")
            if p.role_description:
                lines.append(f"  Description: {p.role_description}")
            lines.append(f'  Send: send_to_agent(target_agent="{p.name}", content="...", invocation_id=null)')
            lines.append("")

        # Show template types for dynamic creation (only for normal agents)
        is_subagent = current_profile is not None and current_profile.comm_kind == AgentCommKind.SUBAGENT
        if templates and not is_subagent:
            existing_names = {p.name for p in targets}
            for t in templates:
                if t.agent_type not in existing_names:
                    lines.append(f"## {t.agent_type}")
                    if t.description:
                        lines.append(f"  Description: {t.description}")
                    lines.append(f'  New task: send_to_agent(target_agent="{t.agent_type}", content="...", invocation_id=null)')
                    lines.append(f'  Continue: send_to_agent(target_agent="{t.agent_type}", content="...", invocation_id="<from previous reply>")')
                    lines.append("")

        # Summary table
        lines.append("## Summary")
        lines.append("| Agent | invocation_id |")
        lines.append("|-------|---------------|")
        for p in targets:
            lines.append(f"| {p.name} | null |")
        if templates and not is_subagent:
            existing_names = {p.name for p in targets}
            for t in templates:
                if t.agent_type not in existing_names:
                    lines.append(f"| {t.agent_type} | null (new) or existing |")

        return "\n".join(lines)
