"""Subagent 配置校验器。

将通用的 subagent 约束规则下沉到框架层，供业务层调用。
"""

import logging

from .descriptor import AgentDescriptor

logger = logging.getLogger(__name__)


class SubagentAgentValidator:
    """Subagent 配置校验器。"""

    @staticmethod
    def validate(descriptor: AgentDescriptor, parent_name: str) -> None:
        """校验 subagent descriptor 是否符合 AgentPool 常驻代理的通用约束。

        Args:
            descriptor: 待校验的 subagent 描述符
            parent_name: 父 agent（主 agent）名称

        Raises:
            ValueError: 当配置存在致命冲突时
        """
        sub_name = descriptor.address.name
        if sub_name == parent_name:
            raise ValueError(
                f"Subagent name '{sub_name}' conflicts with parent_agent_name '{parent_name}'"
            )

        denied = set(descriptor.denied_tools or [])
        if "send_to_agent_async" in denied:
            raise ValueError(
                f"Subagent '{sub_name}' must not deny 'send_to_agent_async' (needed to reply to main)"
            )

        if descriptor.execution_strategy == "pipeline":
            raise ValueError(
                f"Subagent '{sub_name}' uses execution_strategy='pipeline', "
                "which conflicts with AgentPool resident pipeline mechanism. "
                "Use 'react' instead."
            )

        if descriptor.context_strategy != "persistent":
            logger.warning(
                "Subagent '%s' uses non-persistent context_strategy (%s). "
                "Recommended: 'persistent' for AgentPool.",
                sub_name,
                descriptor.context_strategy,
            )

        if not descriptor.system_prompt_template:
            logger.warning(
                "Subagent '%s' has empty system_prompt_template. "
                "Subagents need a clear identity instruction to avoid role confusion.",
                sub_name,
            )
