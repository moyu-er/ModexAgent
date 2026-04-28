"""CheckpointStore — 检查点持久化。

第一阶段：由运行边界的 owner（ReActAgent）显式触发。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class CheckpointStore(Protocol):
    """检查点存储协议。"""

    async def save(self, checkpoint_id: str, data: list[dict[str, Any]]) -> None:
        """保存检查点。"""
        ...

    async def load(self, checkpoint_id: str) -> list[dict[str, Any]] | None:
        """加载检查点，不存在返回 None。"""
        ...

    async def clear(self, checkpoint_id: str) -> None:
        """清除检查点。"""
        ...


class JsonFileCheckpointStore:
    """JSON 文件实现的基本检查点存储。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)

    def _path(self, checkpoint_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in checkpoint_id)
        return self._workspace / f"{safe}.json"

    async def save(self, checkpoint_id: str, data: list[dict[str, Any]]) -> None:
        try:
            self._path(checkpoint_id).write_text(
                json.dumps(data, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("CheckpointStore save failed: %s", checkpoint_id)

    async def load(self, checkpoint_id: str) -> list[dict[str, Any]] | None:
        path = self._path(checkpoint_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("CheckpointStore load failed: %s", checkpoint_id)
            return None

    async def clear(self, checkpoint_id: str) -> None:
        path = self._path(checkpoint_id)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            logger.exception("CheckpointStore clear failed: %s", checkpoint_id)


class NoOpCheckpointStore:
    """不持久化的检查点存储（默认占位）。"""

    async def save(self, checkpoint_id: str, data: list[dict[str, Any]]) -> None:
        pass

    async def load(self, checkpoint_id: str) -> list[dict[str, Any]] | None:
        return None

    async def clear(self, checkpoint_id: str) -> None:
        pass
