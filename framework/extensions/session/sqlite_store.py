"""SQLite 会话存储实现

基于 SQLite 的本地文件会话存储，零配置、单文件、无需外部服务。

依赖:
    pip install aiosqlite  # 异步 SQLite 支持

Example:
    store = SQLiteSessionStore(
        workspace=Path("./workspace"),
        db_name="sessions.db"
    )

    # 保存会话
    session = Session(
        session_id="sess_123",
        agent_id="chart_agent",
        user_id="user_456",
        messages=[{"role": "user", "content": "Hello"}]
    )
    await store.save(session)

    # 获取会话
    retrieved = await store.get("sess_123")

    # 列出用户会话
    sessions = await store.list_by_user("user_456")
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from framework.core.session import Session, SessionStore


class SQLiteSessionStore(SessionStore):
    """
    SQLite 会话存储。

    使用 SQLite 数据库存储会话数据，支持异步操作。

    数据库结构:
        workspace/
        └── sessions.db
            └── sessions 表
                - session_id (PRIMARY KEY)
                - agent_id
                - user_id
                - messages (JSON)
                - metadata (JSON)
                - created_at (ISO format)
                - updated_at (ISO format)

    Attributes:
        workspace: 工作区根目录
        db_path: 数据库文件路径
    """

    def __init__(self, workspace: Path, db_name: str = "sessions.db", create_dirs: bool = True):
        """
        初始化 SQLite 会话存储。

        Args:
            workspace: 工作区根目录
            db_name: 数据库文件名
            create_dirs: 是否自动创建目录
        """
        self.workspace = Path(workspace)
        self.db_path = self.workspace / db_name

        if create_dirs:
            self.workspace.mkdir(parents=True, exist_ok=True)

    async def _get_connection(self):
        """获取数据库连接（异步）"""
        try:
            import aiosqlite
        except ImportError:
            raise ImportError(
                "aiosqlite is required for SQLiteSessionStore. Install with: pip install aiosqlite"
            )

        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def _init_db(self) -> None:
        """初始化数据库表结构"""
        conn = await self._get_connection()
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    user_id TEXT,
                    messages TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

            # 创建索引
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_user_id 
                ON sessions(user_id)
            """)
            await conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_id 
                ON sessions(agent_id)
            """)

            await conn.commit()
        finally:
            await conn.close()

    def _session_to_row(self, session: Session) -> tuple:
        """将会话对象转换为数据库行"""
        return (
            session.session_id,
            session.agent_id,
            session.user_id,
            json.dumps(session.messages, ensure_ascii=False),
            json.dumps(session.metadata, ensure_ascii=False),
            session.created_at.isoformat(),
            session.updated_at.isoformat(),
        )

    def _row_to_session(self, row: sqlite3.Row) -> Session:
        """将数据库行转换为会话对象"""
        return Session(
            session_id=row["session_id"],
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            messages=json.loads(row["messages"]),
            metadata=json.loads(row["metadata"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def get(self, session_id: str) -> Session | None:
        """
        获取会话。

        Args:
            session_id: 会话ID

        Returns:
            会话对象，不存在则返回None
        """
        await self._init_db()
        conn = await self._get_connection()
        try:
            async with conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    return self._row_to_session(row)
                return None
        finally:
            await conn.close()

    async def save(self, session: Session) -> None:
        """
        保存会话。

        如果会话已存在则更新，否则插入新记录。

        Args:
            session: 会话对象
        """
        await self._init_db()
        conn = await self._get_connection()
        try:
            # 更新 updated_at
            session.updated_at = datetime.now()

            # UPSERT 操作
            await conn.execute(
                """
                INSERT INTO sessions 
                (session_id, agent_id, user_id, messages, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    user_id=excluded.user_id,
                    messages=excluded.messages,
                    metadata=excluded.metadata,
                    updated_at=excluded.updated_at
            """,
                self._session_to_row(session),
            )

            await conn.commit()
        finally:
            await conn.close()

    async def delete(self, session_id: str) -> bool:
        """
        删除会话。

        Args:
            session_id: 会话ID

        Returns:
            是否删除成功
        """
        await self._init_db()
        conn = await self._get_connection()
        try:
            async with conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            ) as cursor:
                await conn.commit()
                return cursor.rowcount > 0
        finally:
            await conn.close()

    async def list_by_user(self, user_id: str, limit: int = 10, offset: int = 0) -> list[Session]:
        """
        列出用户的所有会话。

        按更新时间倒序排列。

        Args:
            user_id: 用户ID
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            会话列表
        """
        await self._init_db()
        conn = await self._get_connection()
        try:
            async with conn.execute(
                """
                SELECT * FROM sessions 
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_session(row) for row in rows]
        finally:
            await conn.close()

    async def list_by_agent(self, agent_id: str, limit: int = 10, offset: int = 0) -> list[Session]:
        """
        列出 Agent 的所有会话。

        Args:
            agent_id: Agent ID
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            会话列表
        """
        await self._init_db()
        conn = await self._get_connection()
        try:
            async with conn.execute(
                """
                SELECT * FROM sessions 
                WHERE agent_id = ?
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (agent_id, limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_session(row) for row in rows]
        finally:
            await conn.close()

    async def list_all(self, limit: int = 10, offset: int = 0) -> list[Session]:
        """
        列出所有会话。

        Args:
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            会话列表
        """
        await self._init_db()
        conn = await self._get_connection()
        try:
            async with conn.execute(
                """
                SELECT * FROM sessions 
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
                return [self._row_to_session(row) for row in rows]
        finally:
            await conn.close()

    async def count_by_user(self, user_id: str) -> int:
        """
        统计用户的会话数量。

        Args:
            user_id: 用户ID

        Returns:
            会话数量
        """
        await self._init_db()
        conn = await self._get_connection()
        try:
            async with conn.execute(
                "SELECT COUNT(*) as count FROM sessions WHERE user_id = ?", (user_id,)
            ) as cursor:
                row = await cursor.fetchone()
                return row["count"] if row else 0
        finally:
            await conn.close()

    async def clear_all(self) -> None:
        """清空所有会话数据（谨慎使用）"""
        await self._init_db()
        conn = await self._get_connection()
        try:
            await conn.execute("DELETE FROM sessions")
            await conn.commit()
        finally:
            await conn.close()


class InMemorySessionStore(SessionStore):
    """
    内存会话存储（用于测试）。

    数据仅保存在内存中，进程结束即丢失。
    """

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    async def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    async def save(self, session: Session) -> None:
        session.updated_at = datetime.now()
        self._sessions[session.session_id] = session

    async def delete(self, session_id: str) -> bool:
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    async def list_by_user(self, user_id: str, limit: int = 10, offset: int = 0) -> list[Session]:
        sessions = [s for s in self._sessions.values() if s.user_id == user_id]
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
        return sessions[offset : offset + limit]

    async def clear_all(self) -> None:
        self._sessions.clear()
