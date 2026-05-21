from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from framework.core.tool_manager import Tool, ToolConfig

from .session_id import DefaultSessionIdStrategy, SessionIdStrategy

if TYPE_CHECKING:
    from framework.messaging.broker import MessageBroker
    from framework.multi_agent.address import AgentAddress
    from framework.multi_agent.bus import AgentMessageBus
    from framework.multi_agent.registry import AgentRegistry

logger = logging.getLogger(__name__)

_MAX_DYNAMIC_PEERS = 5
_MAX_DYNAMIC_SPECIALTIES = 3
_MAX_DYNAMIC_DESCRIPTION_LENGTH = 800


def _build_peer_description(profile) -> str:
    parts = [profile.name]
    if profile.role_description:
        parts.append(f" ({profile.role_description})")
    specs = profile.specialties or []
    if specs:
        displayed = specs[:_MAX_DYNAMIC_SPECIALTIES]
        parts.append(f" [{', '.join(displayed)}]")

    # Extended fields (gracefully omitted if not present)
    capabilities_detail = getattr(profile, "capabilities_detail", None)
    if capabilities_detail:
        parts.append(f"\n   能力：{', '.join(capabilities_detail[:3])}")

    example_tasks = getattr(profile, "example_tasks", None)
    if example_tasks:
        parts.append(f"\n   示例任务：{example_tasks[0]}")

    preferred_communication = getattr(profile, "preferred_communication", None)
    if preferred_communication:
        parts.append(f"\n   通信方式：{preferred_communication}")

    return "".join(parts)


def _build_dynamic_description(
    registry: AgentRegistry | None,
    caller_name: str,
    base_description: str,
) -> str:
    if registry is None:
        return base_description
    peers = registry.list_profiles(caller=caller_name)
    # 排除自身
    peers = [p for p in peers if p.name != caller_name]
    if not peers:
        return base_description

    lines = [base_description]
    lines.append("Available peers:")
    peer_descs = [_build_peer_description(p) for p in peers]

    # 长度截断
    if (
        len(peer_descs) > _MAX_DYNAMIC_PEERS
        or sum(len(d) for d in peer_descs) > _MAX_DYNAMIC_DESCRIPTION_LENGTH
    ):
        peer_descs = peer_descs[:_MAX_DYNAMIC_PEERS]

    for desc in peer_descs:
        lines.append(f"- {desc}")
    return "\n".join(lines)


class SendMessageTool(Tool):
    """跨 Agent 主动发送消息工具（直接唤醒目标 Agent 执行新 turn），受 allowed_callers / allowed_targets ACL 保护。"""

    def __init__(
        self,
        broker: MessageBroker,
        self_address: AgentAddress,
        allowed_callers: list[str] | None = None,
        allowed_targets: list[str] | None = None,
        registry: AgentRegistry | None = None,
        session_strategy: SessionIdStrategy | None = None,
    ):
        self._broker = broker
        self._self_address = self_address
        self._allowed_callers = set(allowed_callers) if allowed_callers else None
        self._allowed_targets = set(allowed_targets) if allowed_targets else None
        self._registry = registry
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()
        super().__init__(
            name="send_message",
            description=(
                "Send a message to another agent and trigger it to process immediately.\n\n"
                "IMPORTANT NOTES:\n"
                "1. You only need to provide 'target_agent' (the agent name) and 'content' (the message).\n"
                "   The system will automatically determine your identity as the sender and the current conversation context.\n"
                "2. This tool only delivers the message; it does NOT return a real-time reply "
                "from the target agent. If you expect a response, the target agent must send it back to you "
                "in a separate message using its own communication tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_agent": {"type": "string", "description": "Name of the target agent"},
                    "content": {"type": "string", "description": "Message content"},
                    "message_type": {"type": "string", "default": "agent_message"},
                },
                "required": ["target_agent", "content"],
            },
            config=ToolConfig(),
        )

    def get_dynamic_schema(self, caller_context: dict[str, Any] | None = None) -> dict[str, Any]:
        caller_name = (caller_context or {}).get("agent_name") or (
            self._self_address.name if self._self_address else ""
        )
        description = _build_dynamic_description(
            self._registry,
            caller_name,
            self.description,
        )
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.parameters,
            },
        }

    def _is_allowed(self, caller_context: dict | None = None) -> bool:
        if self._allowed_callers is None:
            return True
        caller = (caller_context or {}).get("agent_name", "")
        return caller in self._allowed_callers

    def _is_target_allowed(self, target_agent: str) -> bool:
        if self._allowed_targets is None:
            return True
        return target_agent in self._allowed_targets

    async def execute(self, **kwargs) -> str:
        target_agent = kwargs.get("target_agent", "")
        content = kwargs.get("content", "")
        message_type = kwargs.get("message_type", "agent_message")
        caller_context = kwargs.get("caller_context")
        conversation_id = kwargs.get("conversation_id", "")
        agent_session_id = kwargs.get("agent_session_id", "")

        # 自动从当前上下文填充 conversation_id（确保 inbox 路由正确）
        if not conversation_id:
            from framework.multi_agent.context import current_conversation_id

            conversation_id = current_conversation_id.get() or ""

        if not conversation_id:
            logger.warning("send_message called without conversation context")

        if not self._is_allowed(caller_context):
            return "Error: send_message is not allowed for this caller."

        if not self._is_target_allowed(target_agent):
            return f"Error: send_message to {target_agent} is not allowed by policy."

        # Registry existence check (second step, after ACL)
        if self._registry is not None:
            available = [p.name for p in self._registry.list_profiles()]
            if target_agent not in available:
                return (
                    f"Error: Target agent '{target_agent}' not found. "
                    f"Available peers: {', '.join(available)}"
                )

        from .address import AgentAddress
        from .envelope import AgentMessageEnvelope

        envelope = AgentMessageEnvelope(
            payload={"content": content, "message_type": message_type},
            source=AgentAddress(kind=self._self_address.kind, name=self._self_address.name),
            target=AgentAddress(kind="agent", name=target_agent),
            message_type=message_type,
            conversation_id=conversation_id,
            agent_session_id=agent_session_id
            or self._session_strategy.target_session(conversation_id, target_agent, self._self_address.name),
        )

        if envelope.target is not None:
            broker_msg = envelope.to_broker_message()
            logger.debug(
                "SendMessageTool: sending from=%s to=%s conv=%s session=%s",
                self._self_address.name,
                target_agent,
                conversation_id,
                envelope.agent_session_id,
            )
            await self._broker.send_to(envelope.target, broker_msg)
            return f"Message sent to {target_agent}."
        return "Error: target agent not specified."


class SendMessageAsyncTool(Tool):
    """跨 Agent 异步发送消息工具（只落入目标 Agent 的 inbox，可选超时后自动唤醒），受 allowed_callers / allowed_targets ACL 保护。"""

    def __init__(
        self,
        broker: MessageBroker,
        self_address: AgentAddress,
        allowed_callers: list[str] | None = None,
        allowed_targets: list[str] | None = None,
        agent_bus: AgentMessageBus | None = None,
        registry: AgentRegistry | None = None,
        wakeup_timeout: float | None = None,
        session_strategy: SessionIdStrategy | None = None,
    ):
        self._broker = broker
        self._self_address = self_address
        self._allowed_callers = set(allowed_callers) if allowed_callers else None
        self._allowed_targets = set(allowed_targets) if allowed_targets else None
        self._agent_bus = agent_bus
        self._registry = registry
        self._wakeup_timeout = wakeup_timeout if wakeup_timeout is not None and wakeup_timeout > 0 else 1.0
        self._session_strategy = session_strategy or DefaultSessionIdStrategy()
        self._wakeup_tasks: set[asyncio.Task] = set()
        super().__init__(
            name="send_message_async",
            description=(
                "Send a message to another agent's inbox asynchronously. The target agent will pull it "
                "during its next turn. If the message is not consumed within a short timeout, "
                "the target agent will be automatically awakened. "
                "IMPORTANT: This tool only queues the message; it does NOT return a real-time reply "
                "from the target agent. If you expect a response, the target agent must send it back to you "
                "in a separate message using its own communication tool."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_agent": {"type": "string", "description": "Name of the target agent"},
                    "content": {"type": "string", "description": "Message content"},
                    "message_type": {"type": "string", "default": "agent_message"},
                },
                "required": ["target_agent", "content"],
            },
            config=ToolConfig(),
        )

    def get_dynamic_schema(self, caller_context: dict[str, Any] | None = None) -> dict[str, Any]:
        caller_name = (caller_context or {}).get("agent_name") or (
            self._self_address.name if self._self_address else ""
        )
        description = _build_dynamic_description(
            self._registry,
            caller_name,
            self.description,
        )
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": description,
                "parameters": self.parameters,
            },
        }

    def _is_allowed(self, caller_context: dict | None = None) -> bool:
        if self._allowed_callers is None:
            return True
        caller = (caller_context or {}).get("agent_name", "")
        return caller in self._allowed_callers

    def _is_target_allowed(self, target_agent: str) -> bool:
        if self._allowed_targets is None:
            return True
        return target_agent in self._allowed_targets

    async def execute(self, **kwargs) -> str:
        target_agent = kwargs.get("target_agent", "")
        content = kwargs.get("content", "")
        message_type = kwargs.get("message_type", "agent_message")
        caller_context = kwargs.get("caller_context")
        conversation_id = kwargs.get("conversation_id", "")
        agent_session_id = kwargs.get("agent_session_id", "")

        # 自动从当前上下文填充 conversation_id（确保 inbox 路由正确）
        if not conversation_id:
            from framework.multi_agent.context import current_conversation_id

            conversation_id = current_conversation_id.get() or ""

        if not conversation_id:
            logger.warning("send_message_async called without conversation context")

        if not self._is_allowed(caller_context):
            return "Error: send_message_async is not allowed for this caller."

        if not self._is_target_allowed(target_agent):
            return f"Error: send_message_async to {target_agent} is not allowed by policy."

        # Registry existence check (second step, after ACL)
        if self._registry is not None:
            available = [p.name for p in self._registry.list_profiles()]
            if target_agent not in available:
                return (
                    f"Error: Target agent '{target_agent}' not found. "
                    f"Available peers: {', '.join(available)}"
                )

        if self._agent_bus is None:
            return "Error: send_message_async requires an AgentMessageBus but none is configured."

        from .address import AgentAddress
        from .envelope import AgentMessageEnvelope

        payload = {"content": content, "message_type": message_type}
        if message_type == "task_request":
            payload["task_prompt"] = content

        envelope = AgentMessageEnvelope(
            payload=payload,
            source=AgentAddress(kind=self._self_address.kind, name=self._self_address.name),
            target=AgentAddress(kind="agent", name=target_agent),
            message_type=message_type,
            conversation_id=conversation_id,
            agent_session_id=agent_session_id
            or self._session_strategy.target_session(conversation_id, target_agent, self._self_address.name),
        )

        inbox_key = self._session_strategy.agent_session(conversation_id, target_agent)
        await self._agent_bus.send_silent(inbox_key, envelope)

        task = asyncio.create_task(
            self._wakeup_if_pending(inbox_key, target_agent, self._wakeup_timeout)
        )
        self._wakeup_tasks.add(task)
        task.add_done_callback(self._wakeup_tasks.discard)

        return f"Async message queued for {target_agent}."

    async def _wakeup_if_pending(
        self, inbox_key: str, target_agent: str, timeout: float
    ) -> None:
        """Wait for timeout and send a broker wakeup if the message is still pending."""
        import logging

        _logger = logging.getLogger(__name__)
        await asyncio.sleep(timeout)
        if self._agent_bus is None:
            return
        try:
            if await self._agent_bus.has_pending(inbox_key):
                _logger.info(
                    "Wakeup: message for %s still pending after %.1fs, sending wakeup",
                    inbox_key,
                    timeout,
                )
                if self._broker is not None:
                    from framework.messaging.broker import Address, BrokerMessage

                    await self._broker.send_to(
                        Address(kind="agent", name=target_agent),
                        BrokerMessage(
                            payload={"_inbox_wakeup": True, "session_id": inbox_key},
                            sender=Address(kind="system", name="send_message_async"),
                        ),
                    )
        except Exception:
            _logger.exception("Wakeup task failed for %s", inbox_key)


