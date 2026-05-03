"""长期记忆配置

提供可配置的记忆开关、归档策略和生命周期管理配置。

Example:
    # 完整配置
    config = MemoryConfig(
        long_term_enabled=True,
        archive_enabled=True,
        lifecycle_policy=LifecyclePolicy(
            max_entries=1000,
            max_age_days=30,
            importance_threshold=0.5,
            auto_cleanup=True
        ),
        storage_backend=StorageBackend.FILE
    )
    
    # 仅归档，不启用长期记忆
    config = MemoryConfig(
        long_term_enabled=False,
        archive_enabled=True
    )
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StorageBackend(Enum):
    """存储后端类型"""
    FILE = "file"           # Markdown 文件
    SQLITE = "sqlite"       # SQLite 数据库
    VECTOR = "vector"       # 向量存储 (FAISS)
    CHROMA = "chroma"       # ChromaDB


@dataclass
class LifecyclePolicy:
    """
    记忆生命周期策略。
    
    控制记忆的存活时间、容量限制和清理策略。
    
    Attributes:
        max_entries: 最大条目数，超过则触发清理
        max_age_days: 最大存活天数，超过则标记为过期
        importance_threshold: 重要性阈值，低于此值的记忆优先清理
        auto_cleanup: 是否自动清理，False 则只标记不删除
        cleanup_interval: 清理检查间隔（消息数）
    """
    max_entries: int = 1000
    max_age_days: int = 30
    importance_threshold: float = 0.3
    auto_cleanup: bool = True
    cleanup_interval: int = 100  # 每100条消息检查一次

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "max_entries": self.max_entries,
            "max_age_days": self.max_age_days,
            "importance_threshold": self.importance_threshold,
            "auto_cleanup": self.auto_cleanup,
            "cleanup_interval": self.cleanup_interval,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LifecyclePolicy":
        """从字典创建"""
        return cls(
            max_entries=data.get("max_entries", 1000),
            max_age_days=data.get("max_age_days", 30),
            importance_threshold=data.get("importance_threshold", 0.3),
            auto_cleanup=data.get("auto_cleanup", True),
            cleanup_interval=data.get("cleanup_interval", 100),
        )


@dataclass
class MemoryConfig:
    """
    记忆系统配置。
    
    控制长期记忆的开关、归档策略和存储后端。
    
    配置组合行为:
    ┌─────────────────┬─────────────────┬─────────────────────────────┐
    │  长期记忆开关    │   归档开关       │          行为               │
    ├─────────────────┼─────────────────┼─────────────────────────────┤
    │      OFF        │      OFF        │  纯工作记忆，不保存任何内容   │
    ├─────────────────┼─────────────────┼─────────────────────────────┤
    │      OFF        │      ON         │  不检索历史，但保存归档文件   │
    ├─────────────────┼─────────────────┼─────────────────────────────┤
    │      ON         │      ON         │  完整长期记忆功能             │
    └─────────────────┴─────────────────┴─────────────────────────────┘
    
    Attributes:
        long_term_enabled: 是否启用长期记忆检索
        archive_enabled: 是否启用归档保存
        lifecycle_policy: 生命周期管理策略
        storage_backend: 存储后端类型
        storage_config: 存储后端特定配置
    """
    long_term_enabled: bool = True
    archive_enabled: bool = True
    lifecycle_policy: LifecyclePolicy = field(default_factory=LifecyclePolicy)
    storage_backend: StorageBackend = StorageBackend.FILE
    storage_config: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """验证配置一致性"""
        # 如果启用了长期记忆，归档也必须启用
        if self.long_term_enabled and not self.archive_enabled:
            raise ValueError(
                "Cannot enable long_term_enabled without archive_enabled. "
                "Long-term memory requires archiving to function."
            )

    @property
    def is_memory_active(self) -> bool:
        """是否激活长期记忆功能"""
        return self.long_term_enabled and self.archive_enabled

    @property
    def is_archive_only(self) -> bool:
        """是否仅归档模式（不检索）"""
        return not self.long_term_enabled and self.archive_enabled

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "long_term_enabled": self.long_term_enabled,
            "archive_enabled": self.archive_enabled,
            "lifecycle_policy": self.lifecycle_policy.to_dict(),
            "storage_backend": self.storage_backend.value,
            "storage_config": self.storage_config,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MemoryConfig":
        """从字典创建"""
        return cls(
            long_term_enabled=data.get("long_term_enabled", True),
            archive_enabled=data.get("archive_enabled", True),
            lifecycle_policy=LifecyclePolicy.from_dict(
                data.get("lifecycle_policy", {})
            ),
            storage_backend=StorageBackend(
                data.get("storage_backend", "file")
            ),
            storage_config=data.get("storage_config", {}),
        )

    @classmethod
    def minimal(cls) -> "MemoryConfig":
        """
        最小化配置 - 仅工作记忆，不保存任何内容。
        
        适用于:
        - 临时会话
        - 隐私敏感场景
        - 测试环境
        """
        return cls(
            long_term_enabled=False,
            archive_enabled=False,
        )

    @classmethod
    def archive_only(cls) -> "MemoryConfig":
        """
        仅归档配置 - 保存但不检索。
        
        适用于:
        - 审计需求
        - 后续分析
        - 不影响当前会话
        """
        return cls(
            long_term_enabled=False,
            archive_enabled=True,
        )

    @classmethod
    def full_featured(cls) -> "MemoryConfig":
        """
        完整功能配置 - 启用所有特性。
        
        适用于:
        - 生产环境
        - 需要完整记忆功能的场景
        """
        return cls(
            long_term_enabled=True,
            archive_enabled=True,
            lifecycle_policy=LifecyclePolicy(),
            storage_backend=StorageBackend.FILE,
        )


# 预定义配置模板
MEMORY_CONFIG_TEMPLATES = {
    "minimal": MemoryConfig.minimal,
    "archive_only": MemoryConfig.archive_only,
    "full_featured": MemoryConfig.full_featured,
}
