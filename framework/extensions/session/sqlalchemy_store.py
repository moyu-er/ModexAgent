"""SQLAlchemy会话存储实现"""

import json
from datetime import datetime, timedelta

from sqlalchemy import Column, DateTime, Index, String, Text, and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

from framework.core.session import Session, SessionStore

Base = declarative_base()


class SessionModel(Base):
    """会话数据模型"""

    __tablename__ = "agent_sessions"

    session_id = Column(String(255), primary_key=True)
    agent_id = Column(String(255), nullable=False, index=True)
    user_id = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())
    expires_at = Column(DateTime, nullable=True)
    messages_json = Column(Text, default="[]")  # JSON字符串
    metadata_json = Column(Text, default="{}")  # JSON字符串

    # 索引
    __table_args__ = (
        Index("idx_user_updated", "user_id", "updated_at"),
        Index("idx_expires", "expires_at"),
    )


class SQLAlchemySessionStore(SessionStore):
    """
    基于SQLAlchemy的会话存储实现。

    支持异步操作,可配置自动过期清理。

    Example:
        # 使用SQLite
        store = SQLAlchemySessionStore("sqlite+aiosqlite:///sessions.db")

        # 使用PostgreSQL
        store = SQLAlchemySessionStore(
            "postgresql+asyncpg://user:pass@localhost/db",
            session_ttl=3600,  # 1小时过期
        )

        session = Session(
            session_id="session_1",
            agent_id="agent_1",
            user_id="user_1",
            messages=[{"role": "user", "content": "hello"}],
        )
        await store.save(session)

        loaded = await store.get("session_1")
    """

    def __init__(
        self,
        database_url: str,
        session_ttl: int | None = None,
        cleanup_interval: int = 3600,
    ):
        """
        初始化SQLAlchemy会话存储。

        Args:
            database_url: 数据库连接URL
            session_ttl: 会话过期时间(秒),None表示永不过期
            cleanup_interval: 清理过期会话的间隔(秒)
        """
        self._database_url = database_url
        self._session_ttl = session_ttl
        self._cleanup_interval = cleanup_interval

        # 创建异步引擎
        self._engine = create_async_engine(
            database_url,
            echo=False,
            future=True,
        )

        # 创建会话工厂
        self._session_factory = async_sessionmaker(
            self._engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

        self._initialized = False

    async def _init_db(self):
        """初始化数据库表"""
        if not self._initialized:
            async with self._engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            self._initialized = True

    async def _cleanup_expired(self, db_session: AsyncSession):
        """清理过期会话"""
        if self._session_ttl is None:
            return

        # 删除过期会话
        stmt = delete(SessionModel).where(
            and_(SessionModel.expires_at < func.now(), SessionModel.expires_at.isnot(None))
        )
        await db_session.execute(stmt)

    def _session_to_model(self, session: Session) -> SessionModel:
        """将Session对象转换为数据库模型"""
        expires_at = None
        if self._session_ttl:
            expires_at = datetime.now() + timedelta(seconds=self._session_ttl)

        return SessionModel(
            session_id=session.session_id,
            agent_id=session.agent_id,
            user_id=session.user_id,
            messages_json=json.dumps(session.messages),
            metadata_json=json.dumps(session.metadata),
            expires_at=expires_at,
        )

    def _model_to_session(self, model: SessionModel) -> Session:
        """将数据库模型转换为Session对象"""
        return Session(
            session_id=model.session_id,
            agent_id=model.agent_id,
            user_id=model.user_id,
            messages=json.loads(model.messages_json) if model.messages_json else [],
            metadata=json.loads(model.metadata_json) if model.metadata_json else {},
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    async def get(self, session_id: str) -> Session | None:
        """
        获取会话。

        Args:
            session_id: 会话ID

        Returns:
            会话对象,不存在或过期则返回None
        """
        await self._init_db()

        async with self._session_factory() as db_session:
            # 清理过期会话
            await self._cleanup_expired(db_session)

            stmt = select(SessionModel).where(
                SessionModel.session_id == session_id,
            )
            result = await db_session.execute(stmt)
            db_model = result.scalar_one_or_none()

            if not db_model:
                return None

            # 检查是否过期
            if db_model.expires_at and db_model.expires_at < datetime.now():
                return None

            return self._model_to_session(db_model)

    async def save(self, session: Session) -> None:
        """
        保存会话。

        Args:
            session: 会话对象
        """
        await self._init_db()

        async with self._session_factory() as db_session:
            # 清理过期会话
            await self._cleanup_expired(db_session)

            # 检查是否已存在
            stmt = select(SessionModel).where(
                SessionModel.session_id == session.session_id,
            )
            result = await db_session.execute(stmt)
            existing = result.scalar_one_or_none()

            # 计算过期时间
            expires_at = None
            if self._session_ttl:
                expires_at = datetime.now() + timedelta(seconds=self._session_ttl)

            if existing:
                # 更新现有会话
                existing.agent_id = session.agent_id
                existing.user_id = session.user_id
                existing.messages_json = json.dumps(session.messages)
                existing.metadata_json = json.dumps(session.metadata)
                existing.expires_at = expires_at
                existing.updated_at = func.now()
            else:
                # 创建新会话
                new_model = SessionModel(
                    session_id=session.session_id,
                    agent_id=session.agent_id,
                    user_id=session.user_id,
                    messages_json=json.dumps(session.messages),
                    metadata_json=json.dumps(session.metadata),
                    expires_at=expires_at,
                )
                db_session.add(new_model)

            await db_session.commit()

    async def delete(self, session_id: str) -> bool:
        """
        删除会话。

        Args:
            session_id: 会话ID

        Returns:
            是否成功删除
        """
        await self._init_db()

        async with self._session_factory() as db_session:
            stmt = delete(SessionModel).where(
                SessionModel.session_id == session_id,
            )
            result = await db_session.execute(stmt)
            await db_session.commit()

            return result.rowcount > 0

    async def list_by_user(
        self,
        user_id: str,
        limit: int = 10,
        offset: int = 0,
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
        await self._init_db()

        async with self._session_factory() as db_session:
            # 清理过期会话
            await self._cleanup_expired(db_session)

            # 构建查询
            stmt = (
                select(SessionModel)
                .where(SessionModel.user_id == user_id)
                .order_by(SessionModel.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )

            result = await db_session.execute(stmt)
            models = result.scalars().all()

            return [self._model_to_session(m) for m in models]

    async def close(self):
        """关闭数据库连接"""
        await self._engine.dispose()
