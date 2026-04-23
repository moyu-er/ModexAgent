from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from framework.core.types import InputMessage


@dataclass
class RouteResult:
    """消息路由结果。"""

    conversation_id: str
    agent_session_id: str
    agent_name: str
    prompt_modifier: str | None = None
    envelope_metadata: dict[str, Any] | None = None
    is_envelope: bool = False


class AgentMessageRouter(ABC):
    """Agent 消息路由器抽象基类。

    决定输入消息进入哪个 agent_session_id，以及是否需要修改 prompt。
    """

    @abstractmethod
    def route(
        self,
        input_msg: InputMessage,
        default_agent_name: str = "main",
    ) -> RouteResult:
        """对输入消息进行路由决策，返回 RouteResult。"""
        ...


class DefaultMeshRouter(AgentMessageRouter):
    """默认网格路由器。

    从 InputMessage.metadata 中提取 conversation_id、agent_session_id、
    message_type 等字段；若缺失则使用 fallback 规则构造默认值。
    """

    def route(
        self,
        input_msg: InputMessage,
        default_agent_name: str = "main",
    ) -> RouteResult:
        metadata = input_msg.metadata or {}
        conversation_id = metadata.get("conversation_id") or input_msg.session_id
        agent_session_id = metadata.get("agent_session_id")
        agent_name = default_agent_name

        if agent_session_id:
            # 尝试从 agent_session_id 解析 agent_name，格式为 "{conversation_id}:{agent_name}"
            parts = agent_session_id.split(":", 1)
            agent_name = parts[1] if len(parts) == 2 else agent_session_id
        else:
            agent_session_id = f"{conversation_id}:{default_agent_name}"

        prompt_modifier = None
        message_type = metadata.get("message_type", "agent_message")
        is_envelope = message_type in ("agent_message", "subagent_result", "rpc_request")

        # 对子 Agent 结果注入来源提示
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
