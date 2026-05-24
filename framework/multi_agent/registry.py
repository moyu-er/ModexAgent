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


class AgentDirectory:
    """Agent 目录实现，支持按能力发现。"""

    def __init__(self) -> None:
        self._descriptors: dict[str, AgentDescriptor] = {}
        self._status: dict[str, AgentState] = {}

    def register(self, descriptor: AgentDescriptor) -> None:
        """注册 Agent 描述符。"""
        self._descriptors[descriptor.address.name] = descriptor
        if descriptor.address.name not in self._status:
            self._status[descriptor.address.name] = AgentState.IDLE

    def unregister(self, name: str) -> bool:
        """注销 Agent 描述符。"""
        if name in self._descriptors:
            del self._descriptors[name]
            self._status.pop(name, None)
            return True
        return False

    def list_agents(self) -> list[AgentDescriptor]:
        return list(self._descriptors.values())

    def get_descriptor(self, name: str) -> AgentDescriptor | None:
        return self._descriptors.get(name)

    def get_status(self, name: str) -> AgentState:
        from .state import AgentState
        return self._status.get(name, AgentState.SHUTDOWN)

    def find_by_capability(self, capability: str) -> list[AgentDescriptor]:
        """按能力标签查找 Agent。"""
        return [
            desc
            for desc in self._descriptors.values()
            if capability in desc.address.capabilities
        ]

    def update_status(self, name: str, state: AgentState) -> None:
        """更新 Agent 状态。"""
        self._status[name] = state
