"""记忆存储扩展

提供基于ChromaDB、FAISS的向量记忆存储，以及配置、生命周期管理和归档功能
"""

# 向量存储
try:
    from .chroma import ChromaMemoryStore
    HAS_CHROMA = True
except ImportError:
    HAS_CHROMA = False
    ChromaMemoryStore = None

try:
    from .faiss_store import FAISSMemoryStore
    HAS_FAISS = True
except ImportError:
    HAS_FAISS = False
    FAISSMemoryStore = None

# 配置和生命周期管理
try:
    from .config import (
        MemoryConfig,
        LifecyclePolicy,
        StorageBackend,
        MEMORY_CONFIG_TEMPLATES,
    )
    HAS_CONFIG = True
except ImportError:
    HAS_CONFIG = False
    MemoryConfig = None
    LifecyclePolicy = None
    StorageBackend = None
    MEMORY_CONFIG_TEMPLATES = None

try:
    from .lifecycle import (
        MemoryLifecycleManager,
        MemoryStatus,
        MemoryEvaluation,
    )
    HAS_LIFECYCLE = True
except ImportError:
    HAS_LIFECYCLE = False
    MemoryLifecycleManager = None
    MemoryStatus = None
    MemoryEvaluation = None

try:
    from .archive import (
        AbstractMemoryArchive,
        FileMemoryArchive,
    )
    HAS_ARCHIVE = True
except ImportError:
    HAS_ARCHIVE = False
    AbstractMemoryArchive = None
    FileMemoryArchive = None

__all__ = []
if HAS_CHROMA:
    __all__.append("ChromaMemoryStore")
if HAS_FAISS:
    __all__.append("FAISSMemoryStore")
if HAS_CONFIG:
    __all__.extend(["MemoryConfig", "LifecyclePolicy", "StorageBackend", "MEMORY_CONFIG_TEMPLATES"])
if HAS_LIFECYCLE:
    __all__.extend(["MemoryLifecycleManager", "MemoryStatus", "MemoryEvaluation"])
if HAS_ARCHIVE:
    __all__.extend(["AbstractMemoryArchive", "FileMemoryArchive"])
