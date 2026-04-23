"""Consolidation engine abstractions for long-term memory updates."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MemoryUpdateMode(StrEnum):
    """长期记忆更新模式。"""

    INCREMENTAL = "incremental"
    APPEND = "append"
    SECTION_REPLACE = "section_replace"
    REPLACE_TEXT = "replace_text"


@dataclass
class MemoryUpdate:
    """长期记忆更新指令。"""

    file_name: str
    content: str
    mode: str = str(MemoryUpdateMode.INCREMENTAL)
    reason: str = ""
    search_text: str = ""  # for replace_text mode: text to find


@dataclass
class ConsolidationResult:
    """整合结果。"""

    soul_updates: list[MemoryUpdate] = field(default_factory=list)
    user_updates: list[MemoryUpdate] = field(default_factory=list)
    memory_updates: list[MemoryUpdate] = field(default_factory=list)
    reasoning: str = ""
    success: bool = True

    @classmethod
    def empty(cls) -> "ConsolidationResult":
        return cls()


class ConsolidationEngine(ABC):
    """整合引擎抽象基类。

    负责将 History Archive 中的摘要条目整合进长期记忆文件。
    实现在线 Consolidator（轻量）和离线 DreamEngine（重型）都是其应用方式，
    但本 ABC 本身不区分在线/离线。
    """

    @abstractmethod
    async def consolidate(
        self,
        scope_key: str,
        new_entries: list[dict[str, Any]],
        existing_memories: dict[str, str],
    ) -> ConsolidationResult:
        """整合新历史条目到长期记忆。

        Args:
            scope_key: 分组键
            new_entries: 新的历史摘要条目列表
            existing_memories: 当前长期记忆文件内容，如 {"SOUL.md": "...", "USER.md": "..."}

        Returns:
            ConsolidationResult: 更新指令集合
        """
        pass
