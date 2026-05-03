"""CheckpointStore — 检查点持久化。

第一阶段：由运行边界的 owner（ReActAgent）显式触发。
第二阶段：data 从 list[dict] 扩展为 dict[str, Any] 结构化存储。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from framework.control.exceptions import TerminationReason

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApprovalDenialContext:
    """审批拒绝时的完整上下文，写入 checkpoint 供恢复分析。"""

    tool_name: str
    tool_call_id: str
    arguments: dict[str, object]
    tier: str
    denied_at: float
    reason: str
    session_id: str
    turn_id: str = ""
    iteration: int = 0


@dataclass
class AgentCheckpoint:
    """结构化 agent 检查点。"""

    checkpoint_id: str
    session_id: str
    turn_id: str = ""
    agent_id: str = ""
    version: int = 1
    messages: list[dict[str, Any]] = field(default_factory=list)
    iteration: int = 0
    termination: TerminationReason | None = None
    denial_context: dict[str, Any] | None = None
    cancelled_tool_ids: list[str] = field(default_factory=list)
    partial_content: str | None = None
    created_at: float = field(default_factory=time.monotonic)


class CheckpointStore(Protocol):
    """检查点存储协议。"""

    async def save(self, checkpoint_id: str, data: dict[str, Any]) -> None:
        """保存结构化 checkpoint。

        data 结构:
            {
                "messages": list[dict[str, Any]],       # 本 turn 新增消息
                "termination": str | None,
                "denial_context": dict | None,
                "cancelled_tool_ids": list[str],
                "iteration": int,
            }
        """
        ...

    async def load(self, checkpoint_id: str) -> dict[str, Any] | None:
        """加载 checkpoint，不存在返回 None。"""
        ...

    async def clear(self, checkpoint_id: str) -> None:
        """清除 checkpoint。"""
        ...


class JsonFileCheckpointStore:
    """JSON 文件实现的基本检查点存储。"""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)

    def _path(self, checkpoint_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in checkpoint_id)
        return self._workspace / f"{safe}.json"

    async def save(self, checkpoint_id: str, data: dict[str, Any]) -> None:
        try:
            self._workspace.mkdir(parents=True, exist_ok=True)
            self._path(checkpoint_id).write_text(
                json.dumps(data, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("CheckpointStore save failed: %s", checkpoint_id)

    async def load(self, checkpoint_id: str) -> dict[str, Any] | None:
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

    async def save(self, checkpoint_id: str, data: dict[str, Any]) -> None:
        pass

    async def load(self, checkpoint_id: str) -> dict[str, Any] | None:
        return None

    async def clear(self, checkpoint_id: str) -> None:
        pass


RuntimeStateStore = CheckpointStore
JsonFileRuntimeStateStore = JsonFileCheckpointStore
NoOpRuntimeStateStore = NoOpCheckpointStore
