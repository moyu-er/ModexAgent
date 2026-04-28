"""ControlChannel — 控制命令通道。

负责命令输入（外部/预设 → agent runtime）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Protocol

from framework.control.types import ControlCommand, ControlScope

logger = logging.getLogger(__name__)


class ControlChannel(Protocol):
    """控制命令通道协议。

    负责命令输入：外部/预设策略 → agent runtime。
    """

    async def send(self, command: ControlCommand) -> None:
        """向 channel 发送控制命令。"""
        ...

    async def drain(self, scope: ControlScope, limit: int = 0) -> Sequence[ControlCommand]:
        """消费 scope 下的所有命令（0 = 无限制）。"""
        ...

    async def peek(self, scope: ControlScope) -> Sequence[ControlCommand]:
        """非破坏性查看 scope 下的所有命令。"""
        ...


class InMemoryControlChannel:
    """内存实现的控制命令通道。"""

    def __init__(self) -> None:
        self._commands: list[ControlCommand] = []
        self._created: dict[str, float] = {}

    async def send(self, command: ControlCommand) -> None:
        self._created[command.command_id] = time.monotonic()
        self._commands.append(command)

    async def drain(self, scope: ControlScope, limit: int = 0) -> Sequence[ControlCommand]:
        matched: list[ControlCommand] = []
        remaining: list[ControlCommand] = []
        now = time.monotonic()

        for cmd in self._commands:
            if not self._scope_matches(cmd.scope, scope):
                remaining.append(cmd)
                continue
            if cmd.ttl_seconds is not None:
                created = self._created.get(cmd.command_id, now)
                if now - created > cmd.ttl_seconds:
                    logger.debug("ControlChannel: expired cmd %s", cmd.command_id)
                    self._created.pop(cmd.command_id, None)
                    continue
            if 0 < limit <= len(matched):
                remaining.append(cmd)
                continue
            self._created.pop(cmd.command_id, None)
            matched.append(cmd)

        self._commands = remaining
        return matched

    async def peek(self, scope: ControlScope) -> Sequence[ControlCommand]:
        return [cmd for cmd in self._commands if self._scope_matches(cmd.scope, scope)]

    @staticmethod
    def _scope_matches(cmd_scope: ControlScope, target: ControlScope) -> bool:
        """检查命令作用域是否匹配目标作用域。"""
        if cmd_scope.session_id != target.session_id:
            return False
        if target.agent_id is not None and cmd_scope.agent_id != target.agent_id:
            return False
        if target.turn_id is not None and cmd_scope.turn_id != target.turn_id:
            return False
        return True
