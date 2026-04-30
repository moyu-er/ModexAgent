"""LLMOutputGuardHook — LLM 输出脱敏 + 风险评估。"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from framework.core.agent import AgentContext

if TYPE_CHECKING:
    from framework.core.types import LLMResponse

logger = logging.getLogger(__name__)

# 常见敏感信息模式
_SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    (r'(?:api[_-]?key|apikey|secret|token|password)\s*[:=]\s*[\S]+', '[REDACTED_CREDENTIAL]'),
    (r'\b\d{16,19}\b', '[REDACTED_CARD]'),
    (r'(?:ssh-|-----BEGIN).*?(?:KEY|PRIVATE KEY)', '[REDACTED_KEY]'),
]


class LLMOutputGuardHook:
    """在 after_llm_response 中检查 LLM 输出，进行脱敏和风险评估。

    风险标记写入 ctx.metadata["_llm_output_risk"]，
    供下游组件（如 ProgressReportHook）决策是否告警。
    """

    def __init__(
        self,
        redact_patterns: list[tuple[str, str]] | None = None,
        risk_keywords: set[str] | None = None,
    ) -> None:
        self._redact_patterns = redact_patterns or _SENSITIVE_PATTERNS
        self._risk_keywords = risk_keywords or {
            "exploit", "vulnerability", "backdoor", "injection",
        }

    async def after_llm_response(
        self,
        ctx: AgentContext,
        response: LLMResponse,
    ) -> None:
        content = response.content or ""
        if not content:
            return

        # 脱敏
        redacted = content
        redact_count = 0
        for pattern, replacement in self._redact_patterns:
            new_redacted, n = re.subn(pattern, replacement, redacted, flags=re.IGNORECASE)
            if n > 0:
                redact_count += n
                redacted = new_redacted

        if redact_count > 0:
            logger.info(
                "LLMOutputGuard: redacted %d sensitive patterns session=%s",
                redact_count, ctx.session_id,
            )
            try:
                object.__setattr__(response, 'content', redacted)
            except (AttributeError, TypeError):
                logger.warning("LLMOutputGuard: cannot modify response.content (frozen/immutable)")

        # 风险评估
        lower = content.lower()
        matched_risks = [kw for kw in self._risk_keywords if kw.lower() in lower]
        if matched_risks:
            ctx.metadata["_llm_output_risk"] = matched_risks
            logger.warning(
                "LLMOutputGuard: risk keywords detected session=%s keywords=%s",
                ctx.session_id, matched_risks,
            )
