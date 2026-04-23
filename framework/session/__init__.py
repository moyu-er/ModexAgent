"""AgentSession 协调层

提供单次请求处理的会话管理，适合 HTTP API 场景。
"""

from .agent_session import AgentSession

__all__ = ["AgentSession"]
