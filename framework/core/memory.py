"""Memory存储抽象基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MemoryEntry:
    """记忆条目"""
    id: str
    content: str
    source_session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    importance: float = 1.0  # 重要性评分，用于记忆筛选


class MemoryStore(ABC):
    """
    记忆存储抽象基类。
    
    用于存储和检索Agent的长期记忆。
    
    Example:
        class InMemoryMemoryStore(MemoryStore):
            def __init__(self):
                self._memories: Dict[str, List[MemoryEntry]] = {}
            
            async def add(self, agent_id: str, entry: MemoryEntry) -> None:
                if agent_id not in self._memories:
                    self._memories[agent_id] = []
                self._memories[agent_id].append(entry)
            
            async def search(self, agent_id: str, query: str, limit: int = 5) -> List[MemoryEntry]:
                # 简单实现：返回最近的记忆
                memories = self._memories.get(agent_id, [])
                return memories[-limit:]
    """

    @abstractmethod
    async def add(self, agent_id: str, entry: MemoryEntry) -> None:
        """
        添加记忆。
        
        Args:
            agent_id: Agent标识
            entry: 记忆条目
        """
        pass

    @abstractmethod
    async def search(
        self,
        agent_id: str,
        query: str,
        limit: int = 5
    ) -> list[MemoryEntry]:
        """
        搜索相关记忆。
        
        Args:
            agent_id: Agent标识
            query: 搜索查询
            limit: 返回结果数量限制
        
        Returns:
            相关记忆条目列表
        """
        pass

    @abstractmethod
    async def get_recent(
        self,
        agent_id: str,
        limit: int = 10
    ) -> list[MemoryEntry]:
        """
        获取最近的记忆。
        
        Args:
            agent_id: Agent标识
            limit: 返回结果数量限制
        
        Returns:
            最近添加的记忆条目列表
        """
        pass

    async def delete(self, agent_id: str, memory_id: str) -> bool:
        """
        删除记忆（可选实现）。
        
        Args:
            agent_id: Agent标识
            memory_id: 记忆ID
        
        Returns:
            是否删除成功
        """
        return False

    async def clear(self, agent_id: str) -> None:
        """
        清空Agent的所有记忆（可选实现）。
        
        Args:
            agent_id: Agent标识
        """
        pass
