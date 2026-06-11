from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.multi_agent.descriptor import AgentDescriptor
    from framework.multi_agent.envelope import AgentMessageEnvelope


class MultiAgentContextBuilder:
    """按 agent_session_id 构建运行时消息上下文。"""

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_envelope: AgentMessageEnvelope,
        agent_descriptor: AgentDescriptor,
        session_summary: str | None = None,
    ) -> list[dict[str, Any]]:
        """构建注入当前消息后的完整 messages 列表。"""
        messages = self._build_system_block(agent_descriptor, session_summary)
        for msg in history:
            messages.append(self._normalize_message(msg))
        messages.append(self._envelope_to_message(current_envelope))
        return messages

    @staticmethod
    def _build_system_block(
        agent_descriptor: AgentDescriptor, session_summary: str | None = None
    ) -> list[dict[str, Any]]:
        parts = []
        if agent_descriptor.system_prompt_template:
            parts.append(agent_descriptor.system_prompt_template)
        if session_summary:
            parts.append(f"Session Summary: {session_summary}")
        if not parts:
            return []
        return [{"role": "system", "content": "\n\n".join(parts)}]

    @staticmethod
    def _normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
        """确保消息格式兼容 LLM API。"""
        if "role" not in msg:
            return {"role": "user", "content": str(msg)}
        return dict(msg)

    @staticmethod
    def _envelope_to_message(envelope: AgentMessageEnvelope) -> dict[str, Any]:
        """将 AgentMessageEnvelope 转换为 user 消息字典。"""
        content = envelope.payload.get("content", "")
        meta = {
            "message_id": envelope.message_id,
            "agent_session_id": envelope.agent_session_id,
            "conversation_id": envelope.conversation_id,
            **envelope.metadata,
        }
        return {
            "role": "user",
            "content": str(content),
            "metadata": meta,
        }
