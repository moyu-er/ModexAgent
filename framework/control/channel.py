"""ControlChannel — 控制命令通道。

负责命令输入（外部/预设 → agent runtime）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Sequence
from typing import Protocol

from framework.control.types import ControlCommand, ControlCommandType, ControlScope

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 300.0


class ControlChannel(Protocol):
    """控制命令通道协议。

    负责命令输入：外部/预设策略 → agent runtime。
    """

    async def send(self, command: ControlCommand) -> None:
        """向 channel 发送控制命令。"""
        ...

    async def drain(
        self,
        scope: ControlScope,
        limit: int = 0,
        command_types: set[ControlCommandType] | None = None,
    ) -> Sequence[ControlCommand]:
        """消费 scope 下匹配的命令。

        Args:
            scope: 目标 session (必填) + 可选 agent_id/turn_id 过滤
            limit: 0 = 无限制
            command_types: 只消费这些类型的命令，None = 全部
        """
        ...

    async def peek(
        self,
        scope: ControlScope,
        command_types: set[ControlCommandType] | None = None,
    ) -> Sequence[ControlCommand]:
        """非破坏性查看 scope 下匹配的命令。"""
        ...

    async def cleanup_session(self, session_id: str) -> None:
        """清理指定 session 的所有命令。在 session 结束时调用，避免内存泄漏。"""
        ...


class InMemoryControlChannel:
    """内存实现的控制命令通道。

    按 session_id → command_type → deque 双层分区存储。
    消费者只从自己关心的类型子队列中消费，不做放回操作。
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL) -> None:
        self._queues: dict[str, dict[ControlCommandType, deque[ControlCommand]]] = (
            defaultdict(lambda: defaultdict(deque))
        )
        self._created: dict[str, float] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def send(self, command: ControlCommand) -> None:
        async with self._lock:
            sid = command.scope.session_id
            self._queues[sid][command.type].append(command)
            self._created[command.command_id] = time.monotonic()

    async def drain(
        self,
        scope: ControlScope,
        limit: int = 0,
        command_types: set[ControlCommandType] | None = None,
    ) -> Sequence[ControlCommand]:
        async with self._lock:
            sid = scope.session_id
            type_queues = self._queues.get(sid)
            if not type_queues:
                return []

            now = time.monotonic()
            matched: list[ControlCommand] = []
            limit_ = limit if limit > 0 else float('inf')  # type: ignore[assignment]

            types_to_scan = (
                list(command_types) if command_types is not None
                else list(type_queues.keys())
            )

            for ct in types_to_scan:
                if len(matched) >= limit_:
                    break
                q = type_queues.get(ct)
                if not q:
                    continue

                expired: list[str] = []
                kept: deque[ControlCommand] = deque()

                while q and len(matched) < limit_:
                    cmd = q.popleft()
                    created_at = self._created.get(cmd.command_id, 0.0)

                    effective_ttl = cmd.ttl_seconds if cmd.ttl_seconds is not None else self._ttl
                    if now - created_at > effective_ttl:
                        expired.append(cmd.command_id)
                        continue

                    if not self._scope_matches(cmd.scope, scope):
                        kept.append(cmd)
                        continue

                    self._created.pop(cmd.command_id, None)
                    matched.append(cmd)

                for cid in expired:
                    self._created.pop(cid, None)
                remaining = list(kept) + list(q)
                type_queues[ct] = deque(remaining) if remaining else deque()

            return matched

    async def peek(
        self,
        scope: ControlScope,
        command_types: set[ControlCommandType] | None = None,
    ) -> Sequence[ControlCommand]:
        async with self._lock:
            sid = scope.session_id
            type_queues = self._queues.get(sid)
            if not type_queues:
                return []

            now = time.monotonic()
            result: list[ControlCommand] = []
            types_to_scan = (
                list(command_types) if command_types is not None
                else list(type_queues.keys())
            )

            for ct in types_to_scan:
                q = type_queues.get(ct)
                if not q:
                    continue
                expired: list[str] = []
                for cmd in q:
                    created_at = self._created.get(cmd.command_id, 0.0)
                    effective_ttl = cmd.ttl_seconds if cmd.ttl_seconds is not None else self._ttl
                    if now - created_at > effective_ttl:
                        expired.append(cmd.command_id)
                        continue
                    if self._scope_matches(cmd.scope, scope):
                        result.append(cmd)
                for cid in expired:
                    self._created.pop(cid, None)
                if expired:
                    expired_set = frozenset(expired)
                    type_queues[ct] = deque(
                        c for c in q if c.command_id not in expired_set
                    )
            return result

    @staticmethod
    def _scope_matches(cmd_scope: ControlScope, target: ControlScope) -> bool:
        if cmd_scope.session_id != target.session_id:
            return False
        if target.agent_id is not None and cmd_scope.agent_id != target.agent_id:
            return False
        return not (target.turn_id is not None and cmd_scope.turn_id != target.turn_id)

    async def cleanup_session(self, session_id: str) -> None:
        async with self._lock:
            type_queues = self._queues.pop(session_id, {})
            for q in type_queues.values():
                for cmd in q:
                    self._created.pop(cmd.command_id, None)
