"""会话存储扩展

提供基于SQLite、SQLAlchemy和内存的会话存储实现
"""

try:
    from .sqlite_store import SQLiteSessionStore, InMemorySessionStore
    HAS_SQLITE = True
except ImportError:
    HAS_SQLITE = False
    SQLiteSessionStore = None
    InMemorySessionStore = None

try:
    from .sqlalchemy_store import SQLAlchemySessionStore
    HAS_SQLALCHEMY = True
except ImportError:
    HAS_SQLALCHEMY = False
    SQLAlchemySessionStore = None

__all__ = []
if HAS_SQLITE:
    __all__.extend(["SQLiteSessionStore", "InMemorySessionStore"])
if HAS_SQLALCHEMY:
    __all__.append("SQLAlchemySessionStore")
