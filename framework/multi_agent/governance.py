from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from framework.multi_agent.descriptor import AgentDescriptor


class ContextGovernancePolicy(ABC):
    """上下文治理策略抽象。"""

    @abstractmethod
    def apply(self, messages: list[dict[str, Any]], descriptor: AgentDescriptor) -> list[dict[str, Any]]:
        """应用治理策略并返回处理后的消息列表。"""
        ...


class FullGovernance(ContextGovernancePolicy):
    """完整治理策略：微压缩、token 预算、截断。

    Tool chain repair (去孤儿/补全缺失 tool 结果) 已统一下沉到
    framework/memory/context_governance.py 的 ToolChainRepairGovernance，
    其在 ReActAgent.run() 每次 LLM 调用前自动生效。

    本策略仅保留上下文窗口管理相关的治理（微压缩、tool result 预算、
    消息预算、历史截断），不再包含重复的 tool chain repair 实现。
    """

    def apply(self, messages: list[dict[str, Any]], descriptor: AgentDescriptor) -> list[dict[str, Any]]:
        config = descriptor.governance_config
        if config.enable_microcompact:
            messages = self._microcompact(messages, keep=config.microcompact_keep_recent)
        if config.enable_budget:
            messages = self._apply_tool_result_budget(messages, config.max_tool_result_chars)
            messages = self._apply_message_budget(messages, config.max_message_chars)
        if config.enable_snip:
            messages = self._snip_history(
                messages,
                descriptor.context_window_tokens,
                keep_recent=config.snip_keep_recent,
            )
        return messages

    @staticmethod
    def _microcompact(messages: list[dict[str, Any]], keep: int) -> list[dict[str, Any]]:
        """对较早的历史消息进行简洁化摘要（v1 实现：仅折叠长 tool_result）。"""
        if len(messages) <= keep * 2:
            return messages
        result = []
        # 保留最近 keep*2 条原样
        head = messages[:-keep * 2]
        tail = messages[-keep * 2:]
        for msg in head:
            if msg.get("role") == "tool" and len(str(msg.get("content", ""))) > 200:
                msg = dict(msg)
                msg["content"] = "<tool result condensed>"
            result.append(msg)
        result.extend(tail)
        return result

    @staticmethod
    def _apply_tool_result_budget(messages: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
        """截断过长的 tool 结果。"""
        result = []
        for msg in messages:
            if msg.get("role") == "tool":
                content = str(msg.get("content", ""))
                if len(content) > max_chars:
                    msg = dict(msg)
                    msg["content"] = content[:max_chars] + "\n... [truncated]"
            result.append(msg)
        return result

    @staticmethod
    def _apply_message_budget(messages: list[dict[str, Any]], max_chars: int | None) -> list[dict[str, Any]]:
        """截断过长的 assistant/user 消息内容。"""
        if max_chars is None:
            return messages
        result = []
        for msg in messages:
            if msg.get("role") in ("assistant", "user"):
                content = str(msg.get("content", ""))
                if len(content) > max_chars:
                    msg = dict(msg)
                    msg["content"] = content[:max_chars] + "\n... [truncated]"
            result.append(msg)
        return result

    @staticmethod
    def _snip_history(
        messages: list[dict[str, Any]],
        context_window_tokens: int | None,
        keep_recent: int,
    ) -> list[dict[str, Any]]:
        """若超出 token 预算则截断较早的历史（v1 用字符数做保守估计）。"""
        if context_window_tokens is None:
            return messages
        # 简单估算：每 4 字符 ≈ 1 token
        total_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        estimated_tokens = total_chars // 4
        if estimated_tokens <= context_window_tokens:
            return messages
        # 保留系统消息和最近 keep_recent 条
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        snipped = non_system[-keep_recent:] if len(non_system) > keep_recent else non_system
        summary = {"role": "system", "content": f"<{len(non_system) - len(snipped)} earlier messages omitted>"}
        return system_msgs + [summary] + snipped


class NoOpGovernance(ContextGovernancePolicy):
    """无操作治理策略。"""

    def apply(self, messages: list[dict[str, Any]], descriptor: AgentDescriptor) -> list[dict[str, Any]]:
        return list(messages)
