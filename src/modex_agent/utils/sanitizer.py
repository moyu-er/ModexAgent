from __future__ import annotations

import re


class ContentSanitizer:
    """内容清洗器：拦截潜在的 prompt 注入和伪造 tool call。"""

    @staticmethod
    def sanitize(text: str) -> str:
        """清洗文本中的危险标签和伪造结构。"""
        # 1. 检测并阻断 <system> 标签（仅匹配独立标签，避免误伤代码比较）
        text = re.sub(
            r"(?i)(^|\s)<\s*system\s*>(\s|$)",
            r"\1[SYSTEM_TAG_BLOCKED]\2",
            text,
        )
        # 2. 检测伪造的 tool_call JSON
        if re.search(r'"tool_calls"\s*:', text):
            text = "[FORGED_TOOL_CALL_BLOCKED]\n" + text
        return text
