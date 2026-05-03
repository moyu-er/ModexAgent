"""ControlStore -- durable control command persistence."""
from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Protocol

from framework.control.types import ControlCommand, ControlCommandType, ControlEvent, ControlScope


class ControlStore(Protocol):
    async def append_command(self, scope: ControlScope, command: ControlCommand) -> None: ...
    async def claim_commands(
        self, scope: ControlScope, *, limit: int = 0,
        command_types: set[ControlCommandType] | None = None,
    ) -> list[ControlCommand]: ...
    async def mark_handled(self, command_id: str, result: dict[str, Any]) -> None: ...
    async def append_event(self, event: ControlEvent) -> None: ...


class InMemoryControlStore:
    def __init__(self) -> None:
        self._commands: dict[str, deque[ControlCommand]] = defaultdict(deque)
        self._events: dict[str, list[ControlEvent]] = defaultdict(list)
        self._handled: dict[str, dict[str, Any]] = {}

    async def append_command(self, scope: ControlScope, command: ControlCommand) -> None:
        self._commands[scope.session_id].append(command)

    async def claim_commands(
        self, scope: ControlScope, *, limit: int = 0,
        command_types: set[ControlCommandType] | None = None,
    ) -> list[ControlCommand]:
        q = self._commands.get(scope.session_id, deque())
        if not q:
            return []
        claimed: list[ControlCommand] = []
        kept: deque[ControlCommand] = deque()
        while q:
            cmd = q.popleft()
            if command_types and cmd.type not in command_types:
                kept.append(cmd)
                continue
            claimed.append(cmd)
            if limit > 0 and len(claimed) >= limit:
                break
        self._commands[scope.session_id] = kept + q
        return claimed

    async def mark_handled(self, command_id: str, result: dict[str, Any]) -> None:
        self._handled[command_id] = result

    async def append_event(self, event: ControlEvent) -> None:
        self._events[event.scope.session_id].append(event)
