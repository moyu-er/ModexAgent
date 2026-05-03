"""Session存储抽象基类"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Session:
    """会话数据"""
    session_id: str
    agent_id: str
    user_id: str | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


class SessionStore(ABC):
    """
    会话存储抽象基类。
    
    用于持久化存储会话数据。
    
    Example:
        class InMemorySessionStore(SessionStore):
            def __init__(self):
                self._sessions: Dict[str, Session] = {}
            
            async def get(self, session_id: str) -> Optional[Session]:
                return self._sessions.get(session_id)
            
            async def save(self, session: Session) -> None:
                session.updated_at = datetime.now()
                self._sessions[session.session_id] = session
    """

    @abstractmethod
    async def get(self, session_id: str) -> Session | None:
        """
        获取会话。
        
        Args:
            session_id: 会话ID
        
        Returns:
            会话对象，不存在则返回None
        """
        pass

    @abstractmethod
    async def save(self, session: Session) -> None:
        """
        保存会话。
        
        Args:
            session: 会话对象
        """
        pass

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """
        删除会话。
        
        Args:
            session_id: 会话ID
        
        Returns:
            是否删除成功
        """
        pass

    @abstractmethod
    async def list_by_user(
        self,
        user_id: str,
        limit: int = 10,
        offset: int = 0
    ) -> list[Session]:
        """
        列出用户的所有会话。
        
        Args:
            user_id: 用户ID
            limit: 返回数量限制
            offset: 偏移量
        
        Returns:
            会话列表
        """
        pass
