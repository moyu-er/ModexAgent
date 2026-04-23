from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Toolset:
    """声明式工具分组，支持通过 ``includes`` 继承其他工具集。"""

    name: str
    description: str = ""
    tool_names: list[str] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


def resolve_toolset(name: str, registry: dict[str, Toolset]) -> list[str]:
    """解析工具集，展开所有继承关系并返回合并后的工具名称列表。

    Args:
        name: 要解析的工具集名称。
        registry: 工具集名称到 Toolset 的映射。

    Returns:
        合并后的唯一工具名称列表（按发现顺序）。

    Raises:
        ValueError: 如果发现循环依赖。
    """
    result: list[str] = []
    visited: set[str] = set()
    stack: list[str] = []

    def _resolve(current: str) -> None:
        if current in stack:
            cycle = " -> ".join(stack[stack.index(current) :] + [current])
            raise ValueError(f"Circular toolset dependency detected: {cycle}")
        if current in visited:
            return
        toolset = registry.get(current)
        if toolset is None:
            return
        stack.append(current)
        for inc in toolset.includes:
            _resolve(inc)
        for tn in toolset.tool_names:
            if tn not in result:
                result.append(tn)
        visited.add(current)
        stack.pop()

    _resolve(name)
    return result
