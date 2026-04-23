"""归档存储抽象与实现

提供记忆的归档功能，支持多种存储后端。

Example:
    # 文件归档
    archive = FileMemoryArchive(
        workspace=Path("./workspace"),
        agent_id="chart_agent"
    )

    # 归档记忆
    await archive.archive(memory_entry)

    # 检索归档
    entry = await archive.retrieve("mem_123")
"""

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from framework.core.memory import MemoryEntry

logger = logging.getLogger(__name__)


class AbstractMemoryArchive(ABC):
    """
    归档存储抽象基类。

    定义归档存储的通用接口，支持多种后端实现。
    """

    @abstractmethod
    async def archive(self, entry: MemoryEntry) -> bool:
        """
        归档记忆。

        Args:
            entry: 记忆条目

        Returns:
            是否成功归档
        """
        pass

    @abstractmethod
    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        """
        检索归档的记忆。

        Args:
            entry_id: 记忆ID

        Returns:
            记忆条目，不存在返回 None
        """
        pass

    @abstractmethod
    async def list_archives(self, limit: int = 100, offset: int = 0) -> List[MemoryEntry]:
        """
        列出所有归档。

        Args:
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            归档的记忆列表
        """
        pass

    @abstractmethod
    async def delete_archive(self, entry_id: str) -> bool:
        """
        删除归档。

        Args:
            entry_id: 记忆ID

        Returns:
            是否成功删除
        """
        pass

    @abstractmethod
    async def clear_all(self) -> None:
        """清空所有归档"""
        pass


class FileMemoryArchive(AbstractMemoryArchive):
    """
    基于文件的归档存储。

    使用 JSON 文件存储归档的记忆，每个记忆一个文件。

    存储结构:
        workspace/memory/
        └── {agent_id}/
            └── archive/
                ├── mem_001.json
                ├── mem_002.json
                └── ...

    Attributes:
        workspace: 工作区根目录
        agent_id: Agent标识
        archive_dir: 归档目录路径
    """

    def __init__(self, workspace: Path, agent_id: str, create_dirs: bool = True):
        """
        初始化文件归档存储。

        Args:
            workspace: 工作区根目录
            agent_id: Agent标识
            create_dirs: 是否自动创建目录
        """
        self.workspace = Path(workspace)
        self.agent_id = agent_id
        self.archive_dir = self.workspace / "memory" / agent_id / "archive"

        if create_dirs:
            self.archive_dir.mkdir(parents=True, exist_ok=True)

    def _entry_to_file_path(self, entry_id: str) -> Path:
        """获取记忆条目对应的文件路径"""
        # 清理 entry_id，确保文件名安全
        safe_id = "".join(c for c in entry_id if c.isalnum() or c in "_-").rstrip()
        return self.archive_dir / f"{safe_id}.json"

    def _entry_to_dict(self, entry: MemoryEntry) -> Dict[str, Any]:
        """将记忆条目转换为字典"""
        return {
            "id": entry.id,
            "content": entry.content,
            "source_session_id": entry.source_session_id,
            "metadata": entry.metadata,
            "created_at": entry.created_at.isoformat(),
            "importance": entry.importance,
            "archived_at": datetime.now().isoformat(),
        }

    def _dict_to_entry(self, data: Dict[str, Any]) -> MemoryEntry:
        """将字典转换为记忆条目"""
        return MemoryEntry(
            id=data["id"],
            content=data["content"],
            source_session_id=data.get("source_session_id"),
            metadata=data.get("metadata", {}),
            created_at=datetime.fromisoformat(data["created_at"]),
            importance=data.get("importance", 1.0),
        )

    async def archive(self, entry: MemoryEntry) -> bool:
        """
        归档记忆到文件。

        Args:
            entry: 记忆条目

        Returns:
            是否成功归档
        """
        try:
            file_path = self._entry_to_file_path(entry.id)
            data = self._entry_to_dict(entry)

            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            logger.warning(f"Failed to archive memory {entry.id}: {e}")
            return False

    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        """
        从文件检索归档的记忆。

        Args:
            entry_id: 记忆ID

        Returns:
            记忆条目，不存在返回 None
        """
        file_path = self._entry_to_file_path(entry_id)

        if not file_path.exists():
            return None

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            return self._dict_to_entry(data)
        except Exception as e:
            logger.warning(f"Failed to retrieve memory {entry_id}: {e}")
            return None

    async def list_archives(self, limit: int = 100, offset: int = 0) -> List[MemoryEntry]:
        """
        列出所有归档的记忆。

        按归档时间倒序排列。

        Args:
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            归档的记忆列表
        """
        entries = []

        if not self.archive_dir.exists():
            return entries

        # 获取所有 JSON 文件
        json_files = sorted(
            self.archive_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )

        # 应用分页
        paginated_files = json_files[offset : offset + limit]

        for file_path in paginated_files:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entry = self._dict_to_entry(data)
                entries.append(entry)
            except Exception as e:
                logger.warning(f"Failed to load archive {file_path}: {e}")
                continue

        return entries

    async def delete_archive(self, entry_id: str) -> bool:
        """
        删除归档文件。

        Args:
            entry_id: 记忆ID

        Returns:
            是否成功删除
        """
        file_path = self._entry_to_file_path(entry_id)

        if not file_path.exists():
            return False

        try:
            file_path.unlink()
            return True
        except Exception as e:
            logger.warning(f"Failed to delete archive {entry_id}: {e}")
            return False

    async def clear_all(self) -> None:
        """清空所有归档文件"""
        if not self.archive_dir.exists():
            return

        for file_path in self.archive_dir.glob("*.json"):
            try:
                file_path.unlink()
            except Exception as e:
                logger.warning(f"Failed to delete {file_path}: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """
        获取归档统计信息。

        Returns:
            统计信息字典
        """
        if not self.archive_dir.exists():
            return {"total_archives": 0, "archive_dir": str(self.archive_dir)}

        json_files = list(self.archive_dir.glob("*.json"))
        total_size = sum(f.stat().st_size for f in json_files)

        return {
            "total_archives": len(json_files),
            "total_size_bytes": total_size,
            "archive_dir": str(self.archive_dir),
        }


class SQLMemoryArchive(AbstractMemoryArchive):
    """
    基于 SQL 数据库的归档存储（预留实现）。

    可用于需要高性能或复杂查询的场景。
    """

    def __init__(self, connection_string: str):
        """
        初始化 SQL 归档存储。

        Args:
            connection_string: 数据库连接字符串
        """
        self.connection_string = connection_string
        # TODO: 实现 SQL 存储
        raise NotImplementedError("SQLMemoryArchive not yet implemented")

    async def archive(self, entry: MemoryEntry) -> bool:
        raise NotImplementedError()

    async def retrieve(self, entry_id: str) -> Optional[MemoryEntry]:
        raise NotImplementedError()

    async def list_archives(self, limit: int = 100, offset: int = 0) -> List[MemoryEntry]:
        raise NotImplementedError()

    async def delete_archive(self, entry_id: str) -> bool:
        raise NotImplementedError()

    async def clear_all(self) -> None:
        raise NotImplementedError()
