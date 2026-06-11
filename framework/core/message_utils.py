"""Agent 消息规范化工具。

处理内部 `role: "agent"` 消息到 LLM 兼容格式的转换。

设计原则：
- 内部存储使用 `role: "agent"` + `source_agent` 字段，语义清晰
- 调用 LLM 前映射为 `role: "user"` + `name` 字段 + 内容前缀
- 系统提示词中声明 agent 消息来源，帮助 LLM 区分人类用户与其他 Agent
"""

from collections.abc import Sequence
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.message import ChatMessage, ContentFormat
from framework.utils.xml import xml_attr, xml_text

AGENT_COMMUNICATION_SYSTEM_NOTE = (
    "\n\n## Agent Messages\n"
    "Messages in <agent_message> or <agent_result> XML format are from other agents, not the human user. "
    "Treat them as input from collaborators. If a response is needed, use your available "
    "communication tool (`send_to_agent`) with `target_agent` set to the sender name."
)


def _msg_to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    """将 ChatMessage 或 dict 统一转换为 dict。"""
    if isinstance(msg, ChatMessage):
        return msg.to_dict()
    return msg


def normalize_agent_messages_for_llm(
    messages: Sequence[ChatMessage | dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """将内部 `role: "agent"` 消息转换为 LLM 可识别的 XML 格式。

    转换规则：
    - `role: "agent"` → `role: "user"` with XML <agent_message> envelope
    - Content wrapped in: <agent_message source="..."><content>...</content></agent_message>
    - content_format set to "xml" for correct truncation handling
    - Other role messages are unaffected

    Args:
        messages: Raw message list (may contain role: "agent"), ChatMessage or dict

    Returns:
        (converted_messages, has_agent_messages) tuple:
        - converted_messages: Converted message list (new list, does not modify original data)
        - has_agent_messages: Whether agent messages are present (used to decide whether to inject system prompt note)
    """
    has_agent = False
    converted: list[dict[str, Any]] = []

    for msg in messages:
        msg_dict = _msg_to_dict(msg)
        if msg_dict.get("role") != MessageRole.AGENT:
            converted.append(msg_dict)
            continue

        has_agent = True
        source_agent = msg_dict.get("source_agent", "unknown")
        original_content = msg_dict.get("content", "")
        ts = msg_dict.get("created_at", "")

        xml_content = (
            f'<agent_message source="{xml_attr(str(source_agent))}"'
            + (f' timestamp="{ts}"' if ts else "")
            + ">\n"
            + f"  <content>{xml_text(str(original_content))}</content>\n"
            + "</agent_message>"
        )

        converted.append(
            {
                "role": MessageRole.USER,
                "content": xml_content,
                "content_format": ContentFormat.XML,
                "truncatable_paths": ["content"],
                "name": msg_dict.get("name"),
                "tool_calls": msg_dict.get("tool_calls"),
                "tool_call_id": msg_dict.get("tool_call_id"),
                "metadata": msg_dict.get("metadata"),
            }
        )

    return converted, has_agent
