"""Peer Agent 配置校验器。

将通用的 peer agent 约束规则下沉到框架层，供业务层调用。
"""

import logging

from .descriptor import AgentDescriptor

logger = logging.getLogger(__name__)


class PeerAgentValidator:
    """Peer Agent 配置校验器。"""

    @staticmethod
    def validate(peer_descriptor: AgentDescriptor, parent_name: str) -> None:
        """校验 peer descriptor 是否符合 AgentPool 常驻代理的通用约束。

        Args:
            peer_descriptor: 待校验的 peer agent 描述符
            parent_name: 父 agent（主 agent）名称

        Raises:
            ValueError: 当配置存在致命冲突时
        """
        peer_name = peer_descriptor.address.name
        if peer_name == parent_name:
            raise ValueError(
                f"Peer name '{peer_name}' conflicts with parent_agent_name '{parent_name}'"
            )

        denied = set(peer_descriptor.denied_tools or [])
        if "send_message_async" in denied:
            raise ValueError(
                f"Peer '{peer_name}' must not deny 'send_message_async' (needed to reply to main)"
            )

        if peer_descriptor.execution_strategy == "pipeline":
            raise ValueError(
                f"Peer '{peer_name}' uses execution_strategy='pipeline', "
                "which conflicts with AgentPool resident pipeline mechanism. "
                "Use 'react' instead."
            )

        if peer_descriptor.context_strategy != "persistent":
            logger.warning(
                "Peer '%s' uses non-persistent context_strategy (%s). "
                "Recommended: 'persistent' for AgentPool.",
                peer_name,
                peer_descriptor.context_strategy,
            )

        if not peer_descriptor.system_prompt_template:
            logger.warning(
                "Peer '%s' has empty system_prompt_template. "
                "Peers need a clear identity instruction to avoid role confusion.",
                peer_name,
            )
