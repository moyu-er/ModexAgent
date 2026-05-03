"""记忆生命周期管理

提供记忆状态评估、自动清理和生命周期管理功能。

Example:
    policy = LifecyclePolicy(
        max_entries=1000,
        max_age_days=30,
        auto_cleanup=True
    )

    manager = MemoryLifecycleManager(policy)

    # 评估记忆状态
    status = manager.evaluate(memory_entry)

    # 执行清理
    removed = await manager.cleanup(memory_store)
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Protocol

from framework.core.memory import MemoryEntry, MemoryStore

from .config import LifecyclePolicy


class MemoryStatus(Enum):
    """记忆状态"""

    ACTIVE = "active"  # 活跃状态，正常使用
    STALE = "stale"  # 陈旧状态，即将过期
    EXPIRED = "expired"  # 已过期，待清理
    ARCHIVED = "archived"  # 已归档


@dataclass
class MemoryEvaluation:
    """记忆评估结果"""

    entry: MemoryEntry
    status: MemoryStatus
    age_days: int
    reason: str  # 状态原因说明

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry.id,
            "status": self.status.value,
            "age_days": self.age_days,
            "reason": self.reason,
        }


class ArchiveStore(Protocol):
    """归档存储协议"""

    async def archive(self, entry: MemoryEntry) -> bool:
        """归档记忆"""
        ...

    async def retrieve(self, entry_id: str) -> MemoryEntry | None:
        """检索归档"""
        ...

    async def list_archives(self, limit: int = 100) -> list[MemoryEntry]:
        """列出归档"""
        ...


class MemoryLifecycleManager:
    """
    记忆生命周期管理器。

    负责:
    1. 评估记忆状态（ACTIVE/STALE/EXPIRED）
    2. 执行生命周期策略（容量/时间/重要性过滤）
    3. 自动或手动清理过期记忆
    4. 归档管理

    Example:
        manager = MemoryLifecycleManager(
            policy=LifecyclePolicy(max_entries=100, max_age_days=7),
            archive_store=FileMemoryArchive(...)
        )

        # 评估单个记忆
        eval_result = manager.evaluate(memory_entry)

        # 批量清理
        removed = await manager.cleanup(memory_store)
    """

    def __init__(
        self,
        policy: LifecyclePolicy,
        archive_store: ArchiveStore | None = None,
    ):
        """
        初始化生命周期管理器。

        Args:
            policy: 生命周期策略
            archive_store: 归档存储（可选）
        """
        self.policy = policy
        self.archive_store = archive_store
        self._message_count = 0  # 消息计数，用于触发清理检查

    def evaluate(self, entry: MemoryEntry) -> MemoryEvaluation:
        """
        评估记忆状态。

        根据策略判断记忆是 ACTIVE、STALE 还是 EXPIRED。

        Args:
            entry: 记忆条目

        Returns:
            评估结果
        """
        now = datetime.now()
        age = now - entry.created_at
        age_days = age.days

        # 检查是否过期（时间）
        if age_days > self.policy.max_age_days:
            return MemoryEvaluation(
                entry=entry,
                status=MemoryStatus.EXPIRED,
                age_days=age_days,
                reason=f"Expired: {age_days} days old (limit: {self.policy.max_age_days})",
            )

        # 检查是否即将过期（80% 时间）
        if age_days > self.policy.max_age_days * 0.8:
            return MemoryEvaluation(
                entry=entry,
                status=MemoryStatus.STALE,
                age_days=age_days,
                reason=f"Stale: {age_days} days old (will expire at {self.policy.max_age_days})",
            )

        # 检查重要性
        if entry.importance < self.policy.importance_threshold:
            return MemoryEvaluation(
                entry=entry,
                status=MemoryStatus.STALE,
                age_days=age_days,
                reason=f"Low importance: {entry.importance} (threshold: {self.policy.importance_threshold})",
            )

        # 活跃状态
        return MemoryEvaluation(
            entry=entry,
            status=MemoryStatus.ACTIVE,
            age_days=age_days,
            reason="Active: within age limit and importance threshold",
        )

    def should_check_cleanup(self) -> bool:
        """
        是否应该检查清理。

        根据 cleanup_interval 决定是否需要检查。

        Returns:
            是否需要检查
        """
        self._message_count += 1
        return self._message_count >= self.policy.cleanup_interval

    def reset_counter(self) -> None:
        """重置消息计数器"""
        self._message_count = 0

    async def cleanup(
        self, memory_store: MemoryStore, agent_id: str, force: bool = False
    ) -> list[MemoryEntry]:
        """
        执行清理。

        根据策略清理过期或低重要性的记忆。

        Args:
            memory_store: 记忆存储
            agent_id: Agent标识
            force: 是否强制清理（忽略 auto_cleanup 设置）

        Returns:
            被清理的记忆列表
        """
        if not force and not self.policy.auto_cleanup:
            return []

        # 获取所有记忆
        all_memories = await memory_store.get_recent(agent_id, limit=10000)

        to_remove = []
        to_archive = []

        for entry in all_memories:
            evaluation = self.evaluate(entry)

            if evaluation.status == MemoryStatus.EXPIRED:
                to_remove.append(entry)
            elif evaluation.status == MemoryStatus.STALE:
                # 陈旧记忆可以选择归档而不是删除
                if self.archive_store:
                    to_archive.append(entry)
                else:
                    to_remove.append(entry)

        # 容量检查 - 如果超过限制，移除最不重要/最旧的
        if len(all_memories) > self.policy.max_entries:
            # 按重要性排序，移除低重要性的
            sorted_by_importance = sorted(
                all_memories,
                key=lambda e: (e.importance, e.created_at),
            )
            overflow = len(all_memories) - self.policy.max_entries
            for entry in sorted_by_importance[:overflow]:
                if entry not in to_remove and entry not in to_archive:
                    if self.archive_store:
                        to_archive.append(entry)
                    else:
                        to_remove.append(entry)

        # 执行归档
        archived = []
        for entry in to_archive:
            if self.archive_store:
                success = await self.archive_store.archive(entry)
                if success:
                    archived.append(entry)
                    await memory_store.delete(agent_id, entry.id)

        # 执行删除
        removed = []
        for entry in to_remove:
            success = await memory_store.delete(agent_id, entry.id)
            if success:
                removed.append(entry)

        self.reset_counter()

        return removed + archived

    async def archive_entry(
        self,
        entry: MemoryEntry,
    ) -> bool:
        """
        归档单个记忆。

        Args:
            entry: 记忆条目

        Returns:
            是否成功归档
        """
        if self.archive_store is None:
            return False

        return await self.archive_store.archive(entry)

    async def restore_entry(
        self,
        entry_id: str,
    ) -> MemoryEntry | None:
        """
        从归档恢复记忆。

        Args:
            entry_id: 记忆ID

        Returns:
            恢复的记忆，如果不存在返回 None
        """
        if self.archive_store is None:
            return None

        return await self.archive_store.retrieve(entry_id)

    def get_stats(self, evaluations: list[MemoryEvaluation]) -> dict[str, Any]:
        """
        获取评估统计信息。

        Args:
            evaluations: 评估结果列表

        Returns:
            统计信息字典
        """
        total = len(evaluations)
        status_counts = {
            MemoryStatus.ACTIVE: 0,
            MemoryStatus.STALE: 0,
            MemoryStatus.EXPIRED: 0,
            MemoryStatus.ARCHIVED: 0,
        }

        for eval in evaluations:
            status_counts[eval.status] += 1

        return {
            "total": total,
            "active": status_counts[MemoryStatus.ACTIVE],
            "stale": status_counts[MemoryStatus.STALE],
            "expired": status_counts[MemoryStatus.EXPIRED],
            "archived": status_counts[MemoryStatus.ARCHIVED],
            "active_ratio": status_counts[MemoryStatus.ACTIVE] / total if total > 0 else 0,
        }
