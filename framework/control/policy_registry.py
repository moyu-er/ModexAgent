"""Task supervision policy registry.

Absorbed from the former multi_agent/policy_registry.py module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .task_supervision import TaskSupervisionPolicy


class SupervisionPolicyRegistry:
    """监督策略注册表。"""

    _registry: dict[str, type[TaskSupervisionPolicy]] = {}

    @classmethod
    def register(cls, policy_type: str, policy_class: type[TaskSupervisionPolicy]) -> None:
        cls._registry[policy_type] = policy_class

    @classmethod
    def get(cls, policy_type: str) -> type[TaskSupervisionPolicy]:
        if policy_type not in cls._registry:
            raise ValueError(f"Unknown supervision policy type: {policy_type}")
        return cls._registry[policy_type]


class SupervisionPolicySpec:
    """监督策略配置规格。"""

    def __init__(self, policy_type: str, config: dict[str, Any] | None = None):
        self.policy_type = policy_type
        self.config = config or {}

    def to_policy(self) -> TaskSupervisionPolicy:
        policy_class = SupervisionPolicyRegistry.get(self.policy_type)
        return policy_class.from_config(self.config)
