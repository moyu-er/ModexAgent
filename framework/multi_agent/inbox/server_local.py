"""基于本地文件的 Inbox Server 实现。"""

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from .server import InboxServer
from .tracker import DeliveredIdTracker, FileDeliveredIdTracker
from .types import InboxMessage

logger = logging.getLogger(__name__)


def _safe_dir_name(session_id: str) -> str:
    safe = re.sub(r'[^\w\-.]', '_', session_id)
    if len(safe) > 200:
        import base64

        return base64.urlsafe_b64encode(session_id.encode()).decode()[:200]
    return safe


class LocalFileInboxServer(InboxServer):
    """基于本地文件的 Inbox MQ Server 实现。

    存储路径:
        {workspace}/{safe_session_id}/
            ├── pending.jsonl
            └── delivered_ids.json

    并发安全:
        每个 session_id 一个 asyncio.Lock（单进程安全）。
    """

    def __init__(
        self,
        workspace: Path,
        tracker: DeliveredIdTracker | None = None,
    ) -> None:
        self._workspace = Path(workspace)
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._tracker = tracker or FileDeliveredIdTracker(workspace)

    def _get_lock(self, session_id: str) -> asyncio.Lock:
        return self._locks.setdefault(session_id, asyncio.Lock())

    def _session_dir(self, session_id: str) -> Path:
        return self._workspace / _safe_dir_name(session_id)

    def _pending_path(self, session_dir: Path) -> Path:
        return session_dir / "pending.jsonl"

    async def receive(self, session_id: str, message: InboxMessage) -> bool:
        """幂等接收：检查 pending 队列和 delivered_ids，重复则忽略。"""
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)

        async with self._get_lock(session_id):
            # 检查 delivered_ids
            delivered_ids = await self._tracker.load(session_id)
            if message.message_id in delivered_ids:
                return False

            # 检查 pending 队列中是否已存在
            if pending_path.exists():
                text = pending_path.read_text(encoding="utf-8")
                for line in text.strip().split("\n"):
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("message_id") == message.message_id:
                        return False

            # 新消息，追加到 pending
            session_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "message_id": message.message_id,
                    "source": message.source,
                    "content": message.content,
                    "message_type": message.message_type,
                    "timestamp": message.timestamp.isoformat(),
                    "metadata": message.metadata,
                },
                ensure_ascii=False,
            )
            with open(pending_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            return True

    async def consume(self, session_id: str, limit: int = 100) -> list[InboxMessage]:
        """原子性消费：读取 pending，将 message_id 写入 delivered_ids，未消费的消息保留。"""
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)

        async with self._get_lock(session_id):
            if not pending_path.exists():
                return []

            text = pending_path.read_text(encoding="utf-8")
            lines = [l for l in text.strip().split("\n") if l.strip()]
            consume_lines = lines[:limit]
            remain_lines = lines[limit:]

            pending_path.write_text(
                "\n".join(remain_lines) + "\n" if remain_lines else "",
                encoding="utf-8",
            )

            delivered_ids = await self._tracker.load(session_id)

            messages = []
            for line in consume_lines:
                data = json.loads(line)
                messages.append(
                    InboxMessage(
                        session_id=session_id,
                        source=data["source"],
                        content=data["content"],
                        message_type=data["message_type"],
                        message_id=data["message_id"],
                        timestamp=datetime.fromisoformat(data["timestamp"]),
                        metadata=data.get("metadata", {}),
                    )
                )
                delivered_ids.add(data["message_id"])

            await self._tracker.save(session_id, delivered_ids)

        return messages

    async def peek(self, session_id: str) -> list[InboxMessage]:
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)
        if not pending_path.exists():
            return []
        async with self._get_lock(session_id):
            text = pending_path.read_text(encoding="utf-8")
        messages = []
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            data = json.loads(line)
            messages.append(
                InboxMessage(
                    session_id=session_id,
                    source=data["source"],
                    content=data["content"],
                    message_type=data["message_type"],
                    message_id=data["message_id"],
                    timestamp=datetime.fromisoformat(data["timestamp"]),
                    metadata=data.get("metadata", {}),
                )
            )
        return messages

    async def count(self, session_id: str) -> int:
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)
        if not pending_path.exists():
            return 0
        async with self._get_lock(session_id):
            text = pending_path.read_text(encoding="utf-8")
        return sum(1 for line in text.split("\n") if line.strip())

    async def clear(self, session_id: str) -> None:
        session_dir = self._session_dir(session_id)
        pending_path = self._pending_path(session_dir)
        async with self._get_lock(session_id):
            if pending_path.exists():
                pending_path.write_text("", encoding="utf-8")
            await self._tracker.clear(session_id)

    async def list_sessions(self) -> list[str]:
        """扫描工作目录，返回所有存在 pending.jsonl 文件的会话 ID。"""
        sessions = []
        for item in self._workspace.iterdir():
            if item.is_dir() and (item / "pending.jsonl").exists():
                # 尝试从安全目录名还原原始 session_id
                sessions.append(
                    self._session_id_from_pending(item / "pending.jsonl")
                    or self._unsafe_dir_name(item.name)
                )
        return sessions

    def _session_id_from_pending(self, pending_path: Path) -> str | None:
        """Read the original session ID from pending message metadata."""
        try:
            text = pending_path.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            metadata = data.get("metadata")
            if isinstance(metadata, dict):
                agent_session_id = metadata.get("agent_session_id")
                if isinstance(agent_session_id, str) and agent_session_id:
                    return agent_session_id
        return None

    def _unsafe_dir_name(self, safe_name: str) -> str:
        """尝试将安全目录名还原为原始 session_id。"""
        # 如果长度达到 200，可能是 base64 编码
        if len(safe_name) >= 200:
            try:
                import base64
                return base64.urlsafe_b64decode(safe_name.encode()).decode()
            except Exception:
                pass
        return safe_name
