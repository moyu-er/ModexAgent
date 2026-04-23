"""内存会话存储实现"""

import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional
from collections import defaultdict

from framework.core.session import SessionStore, Session


class InMemorySessionStore(SessionStore):
    """
    基于内存的会话存储实现。

    适用于测试和开发环境,数据在内存中,重启后丢失。

    Example:
        store = InMemorySessionStore(session_ttl=3600)

        session = Session(
            session_id="session_1",
            agent_id="agent_1",
            user_id="user_1",
            messages=[{"role": "user", "content": "hello"}],
        )
        await store.save(session)

        loaded = await store.get("session_1")
    """

    def __init__(self, session_ttl: Optional[int] = None):
        """
        初始化内存会话存储。

        Args:
            session_ttl: 会话过期时间(秒),None表示永不过期
        """
        self._session_ttl = session_ttl
        # 存储结构: {session_id: {session, expires_at}}
        self._sessions: Dict[str, Dict] = {}

    def _cleanup_expired(self):
        """清理过期会话"""
        if self._session_ttl is None:
            return

        now = datetime.now()
        expired_sessions = [
            session_id
            for session_id, session_data in self._sessions.items()
            if session_data.get("expires_at") and session_data["expires_at"] < now
        ]

        for session_id in expired_sessions:
            del self._sessions[session_id]

    async def get(self, session_id: str) -> Optional[Session]:
        """
        获取会话。

        Args:
            session_id: 会话ID

        Returns:
            会话对象,不存在或过期则返回None
        """
        # 清理过期会话
        self._cleanup_expired()

        session_data = self._sessions.get(session_id)
        if not session_data:
            return None

        # 检查是否过期
        if session_data.get("expires_at") and session_data["expires_at"] < datetime.now():
            del self._sessions[session_id]
            return None

        return session_data["session"]

    async def save(self, session: Session) -> None:
        """
        保存会话。

        Args:
            session: 会话对象
        """
        # 清理过期会话
        self._cleanup_expired()

        # 计算过期时间
        expires_at = None
        if self._session_ttl:
            expires_at = datetime.now() + timedelta(seconds=self._session_ttl)

        # 更新会话时间
        session.updated_at = datetime.now()

        # 保存会话
        self._sessions[session.session_id] = {
            "session": session,
            "expires_at": expires_at,
        }

    async def delete(self, session_id: str) -> bool:
        """
        删除会话。

        Args:
            session_id: 会话ID

        Returns:
            是否成功删除
        """
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    async def list_by_user(
        self,
        user_id: str,
        limit: int = 10,
        offset: int = 0,
    ) -> List[Session]:
        """
        列出用户的所有会话。

        Args:
            user_id: 用户ID
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            会话列表
        """
        # 清理过期会话
        self._cleanup_expired()

        # 过滤用户会话
        user_sessions = [
            data["session"]
            for data in self._sessions.values()
            if data["session"].user_id == user_id
        ]

        # 按更新时间排序
        user_sessions.sort(key=lambda s: s.updated_at, reverse=True)

        # 分页
        return user_sessions[offset : offset + limit]
