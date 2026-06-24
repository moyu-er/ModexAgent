"""Delivered ID Tracker 抽象与实现。"""

import json
from abc import ABC, abstractmethod
from pathlib import Path

from modex_agent.utils.file_io import read_json_robust

MAX_DELIVERED_IDS = 10000


class DeliveredIdTracker(ABC):
    """抽象已交付消息 ID 追踪器。"""

    @abstractmethod
    async def load(self, session_id: str) -> set[str]:
        """加载指定 session 的已交付 ID 集合。"""
        ...

    @abstractmethod
    async def save(self, session_id: str, ids: set[str]) -> None:
        """保存指定 session 的已交付 ID 集合。"""
        ...

    @abstractmethod
    async def add(self, session_id: str, message_id: str) -> None:
        """添加单个已交付 ID 并持久化。"""
        ...

    @abstractmethod
    async def clear(self, session_id: str) -> None:
        """清空指定 session 的已交付记录。"""
        ...


class FileDeliveredIdTracker(DeliveredIdTracker):
    """基于本地文件的 DeliveredIdTracker 实现。"""

    def __init__(self, workspace: Path, max_ids: int = MAX_DELIVERED_IDS) -> None:
        self._workspace = Path(workspace)
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._max_ids = max_ids

    def _session_dir(self, session_id: str) -> Path:
        from .server_local import _safe_dir_name

        return self._workspace / _safe_dir_name(session_id)

    def _delivered_path(self, session_dir: Path) -> Path:
        return session_dir / "delivered_ids.json"

    def _load(self, delivered_path: Path) -> set[str]:
        data = read_json_robust(delivered_path)
        if not data:
            return set()
        return set(data.get("ids", []))

    def _save(self, delivered_path: Path, ids: set[str]) -> None:
        delivered_path.parent.mkdir(parents=True, exist_ok=True)
        delivered_path.write_text(
            json.dumps({"ids": list(ids)}, ensure_ascii=False),
            encoding="utf-8",
        )

    async def load(self, session_id: str) -> set[str]:
        return self._load(self._delivered_path(self._session_dir(session_id)))

    async def save(self, session_id: str, ids: set[str]) -> None:
        delivered_path = self._delivered_path(self._session_dir(session_id))
        if len(ids) > self._max_ids:
            ids = set(list(ids)[-self._max_ids :])
        self._save(delivered_path, ids)

    async def add(self, session_id: str, message_id: str) -> None:
        delivered_path = self._delivered_path(self._session_dir(session_id))
        ids = self._load(delivered_path)
        ids.add(message_id)
        if len(ids) > self._max_ids:
            ids = set(list(ids)[-self._max_ids :])
        self._save(delivered_path, ids)

    async def clear(self, session_id: str) -> None:
        delivered_path = self._delivered_path(self._session_dir(session_id))
        if delivered_path.exists():
            delivered_path.write_text(json.dumps({"ids": []}, ensure_ascii=False), encoding="utf-8")
