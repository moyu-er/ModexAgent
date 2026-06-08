from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from framework.multi_agent.comm_kind import AgentCommKind

from .state import AgentState

if TYPE_CHECKING:
    from .descriptor import AgentDescriptor


@dataclass
class AgentProfile:
    """Agent 的发现画像（只读）。"""

    name: str
    role_description: str = ""
    specialties: list[str] | None = None
    status: AgentState | None = None
    allowed_tools: list[str] | None = None
    allowed_skills: list[str] | None = None
    capabilities: list[str] | None = None
    exposed_to_agents: bool = True
    comm_kind: AgentCommKind = AgentCommKind.NORMAL


class AgentRegistry(Protocol):
    """Agent 注册表协议（只读发现层）。"""

    def list_agents(self) -> list[AgentDescriptor]:
        """列出所有已注册 Agent 的描述符。"""
        ...

    def get_descriptor(self, name: str) -> AgentDescriptor | None:
        """按名称获取 Agent 描述符。"""
        ...

    def get_status(self, name: str) -> AgentState:
        """按名称获取 Agent 状态。"""
        ...

    def list_profiles(self, caller: str | None = None) -> list[AgentProfile]:
        """列出对 caller 可见的所有 Agent 画像。"""
        ...

    def get_profile(self, name: str) -> AgentProfile | None:
        """按名称获取 Agent 画像。"""
        ...

    def find_profiles(
        self,
        capability: str | None = None,
        skill: str | None = None,
        tool: str | None = None,
        caller: str | None = None,
    ) -> list[AgentProfile]:
        """按能力、技能或工具筛选可见的 Agent 画像。

        匹配规则：
        - capability / skill / tool 为 None 表示不过滤（通配符）。
        - 空列表 [] 表示要求该字段为空，即 deny-all。
        - 否则要求精确匹配（值存在于对应列表中）。
        """
        ...


