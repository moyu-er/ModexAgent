"""Agent 消息规范化工具。

处理内部 `role: "agent"` 消息到 LLM 兼容格式的转换。

设计原则：
- 内部存储使用 `role: "agent"` + `source_agent` 字段，语义清晰
- 调用 LLM 前映射为 `role: "user"` + `name` 字段 + 内容前缀
- 系统提示词中声明 agent 消息来源，帮助 LLM 区分人类用户与其他 Agent
"""

import copy
from collections.abc import Sequence
from typing import Any

from framework.core.types import MessageRole
from framework.memory.core.message import ChatMessage

AGENT_COMMUNICATION_SYSTEM_NOTE = (
    "\n\n## Agent Messages\n"
    "Messages prefixed with `[From Agent <name>]` are from other agents, not the human user. "
    "Treat them as input from collaborators. If a response is needed, use your available "
    "communication tool (`send_to_agent` or `send_to_agent_async`) with `target_agent` set to the sender name."
)


def agent_source_prefix(source_agent: str) -> str:
    return f"[From Agent {source_agent}]\n"


def ensure_agent_source_prefix(
    content: str | list[dict[str, Any]] | None,
    source_agent: str,
) -> str | list[dict[str, Any]]:
    prefix = agent_source_prefix(source_agent)
    if content is None:
        return prefix
    if isinstance(content, list):
        new_content = copy.deepcopy(content)
        for block in new_content:
            if block.get("type") == "text":
                text = str(block.get("text", ""))
                if not text.startswith(prefix):
                    block["text"] = prefix + text
                return new_content
        new_content.insert(0, {"type": "text", "text": prefix})
        return new_content
    if isinstance(content, str) and content.startswith(prefix):
        return content
    return prefix + str(content)


def _msg_to_dict(msg: ChatMessage | dict[str, Any]) -> dict[str, Any]:
    """将 ChatMessage 或 dict 统一转换为 dict。"""
    if isinstance(msg, ChatMessage):
        return msg.to_dict()
    return msg


def normalize_agent_messages_for_llm(
    messages: Sequence[ChatMessage | dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """将内部 `role: "agent"` 消息转换为 LLM 可识别的格式。

    转换规则：
    - `role: "agent"` → `role: "user"`
    - Content prefix: `[From Agent {source_agent}]\n{original_content}`
    - No `name` field is set, to avoid OpenAI API constraints requiring all user messages to have the same name
    - Other role messages are unaffected

    支持多模态 content（list[dict]）：当 content 为列表时，在第一个 text block 前插入前缀。

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
        original_content = msg_dict.get("content", "")

        converted_msg: dict[str, Any] = {
            "role": MessageRole.USER,
            "content": original_content,
        }

        # 保留 meta_* 字段用于溯源
        for key, value in msg_dict.items():
            if key.startswith("meta_"):
                converted_msg[key] = value

        converted.append(converted_msg)

    return converted, has_agent
