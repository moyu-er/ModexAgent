"""Default semantic message filter implementation."""

from typing import Any

from framework.memory.compression.strategy import (
    MessageFilterStrategy,
    MessageSemanticValue,
)

# 工具白名单：这些 tool 结果被视为具有中等语义价值，值得保留
MEDIUM_TOOL_NAMES = {
    "web_search",
    "ask_user",
    "read_file",
    "edit_file",
    "write_file",
}


class SemanticMessageFilter(MessageFilterStrategy):
    """默认语义过滤器。

    分类规则：
    - user / system → HIGH
    - assistant（无 tool_calls） → HIGH
    - assistant（有 tool_calls） → DERIVED
    - tool（来自 MEDIUM 白名单） → MEDIUM
    - tool（其他） → LOW
    """

    def classify(self, msg: dict[str, Any]) -> MessageSemanticValue:
        role = msg.get("role")
        if role in ("user", "system"):
            return MessageSemanticValue.HIGH
        if role == "assistant":
            if msg.get("tool_calls"):
                return MessageSemanticValue.DERIVED
            return MessageSemanticValue.HIGH
        if role == "tool":
            tool_name = msg.get("name") or ""
            if tool_name in MEDIUM_TOOL_NAMES:
                return MessageSemanticValue.MEDIUM
            return MessageSemanticValue.LOW
        return MessageSemanticValue.LOW

    def sanitize(
        self, messages: list[dict], *, collapse_orphan_chains: bool = True
    ) -> list[dict]:
        values = [self.classify(m) for m in messages]
        keep = [False] * len(messages)

        # 1. 标记 HIGH 与 MEDIUM
        for i, v in enumerate(values):
            if v in (MessageSemanticValue.HIGH, MessageSemanticValue.MEDIUM):
                keep[i] = True

        # 2. 处理 DERIVED（tool_calls）及其 chain
        i = 0
        while i < len(messages):
            if values[i] == MessageSemanticValue.DERIVED:
                tool_calls = messages[i].get("tool_calls", [])
                call_ids = {tc.get("id") for tc in tool_calls if tc.get("id")}

                # 收集紧随其后的 tool result 索引
                result_indices: list[int] = []
                j = i + 1
                while (
                    j < len(messages)
                    and messages[j].get("role") == "tool"
                    and messages[j].get("tool_call_id") in call_ids
                ):
                    result_indices.append(j)
                    j += 1

                kept_results = [
                    idx for idx in result_indices if values[idx] != MessageSemanticValue.LOW
                ]

                if kept_results:
                    # 有结果被保留（MEDIUM），则保留 call 和这些结果
                    keep[i] = True
                    for idx in kept_results:
                        keep[idx] = True
                elif not result_indices:
                    # 链尚未完成（没有结果），保留 call
                    keep[i] = True
                # 否则：有结果但全为 LOW —— 孤儿链，后续折叠或丢弃

                i = j if result_indices else i + 1
            else:
                i += 1

        # 3. 构建 sanitized 列表，折叠或丢弃孤儿链
        sanitized: list[dict] = []
        i = 0
        while i < len(messages):
            if values[i] == MessageSemanticValue.DERIVED and not keep[i]:
                tool_calls = messages[i].get("tool_calls", [])
                if collapse_orphan_chains:
                    tool_names: list[str] = []
                    for tc in tool_calls:
                        if isinstance(tc.get("function"), dict):
                            tool_names.append(tc["function"].get("name", "unknown"))
                        else:
                            tool_names.append(tc.get("name", "unknown"))
                    hint = (
                        f"[Called tools: {', '.join(tool_names)}]"
                        if tool_names
                        else "[Called tools]"
                    )
                    original_content = messages[i].get("content") or ""
                    content = f"{original_content} {hint}".strip() if original_content else hint
                    if content.strip():
                        sanitized.append({"role": "assistant", "content": content})
                # 跳过对应的孤儿 tool results
                call_ids = {tc.get("id") for tc in tool_calls if tc.get("id")}
                j = i + 1
                while (
                    j < len(messages)
                    and messages[j].get("role") == "tool"
                    and messages[j].get("tool_call_id") in call_ids
                ):
                    j += 1
                i = j
                continue
            elif keep[i]:
                sanitized.append(dict(messages[i]))
                i += 1
            else:
                i += 1

        # 4. 移除 stray tool results（call 已被整体移除后的残留 result）
        valid_call_ids: set[str] = set()
        for m in sanitized:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    if tc.get("id"):
                        valid_call_ids.add(tc["id"])

        final: list[dict] = []
        for m in sanitized:
            if m.get("role") == "tool" and m.get("tool_call_id") not in valid_call_ids:
                continue
            final.append(m)

        return final
