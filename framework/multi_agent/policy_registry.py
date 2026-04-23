from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .intervention import TaskInterventionPolicy


class PolicyRegistry:
    """全局策略注册表。"""

    _registry: dict[str, type[TaskInterventionPolicy]] = {}

    @classmethod
    def register(cls, policy_type: str, policy_class: type[TaskInterventionPolicy]) -> None:
        cls._registry[policy_type] = policy_class

    @classmethod
    def get(cls, policy_type: str) -> type[TaskInterventionPolicy]:
        if policy_type not in cls._registry:
            raise KeyError(f"Policy type '{policy_type}' not registered")
        return cls._registry[policy_type]

    @classmethod
    def list_types(cls) -> list[str]:
        return list(cls._registry.keys())


@dataclass
class TaskInterventionPolicySpec:
    """策略的可序列化描述。"""

    policy_type: str
    config: dict[str, Any] = field(default_factory=dict)

    def to_policy(self) -> TaskInterventionPolicy:
        """将 spec 转换为策略实例。"""
        registry = PolicyRegistry.get(self.policy_type)
        return registry.from_config(self.config)
