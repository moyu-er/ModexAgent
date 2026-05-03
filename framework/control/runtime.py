"""ControlRuntime -- safe-boundary control command plane."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.core.agent import AgentContext
    from framework.control.channel import ControlChannel
    from framework.control.store import ControlStore
    from framework.interceptor.handler import CommandHandlerRegistry

logger = logging.getLogger(__name__)


class ControlPhase(StrEnum):
    BEFORE_TURN = "before_turn"
    BEFORE_ITERATION = "before_iteration"
    BEFORE_LLM = "before_llm"
    BEFORE_TOOL_BATCH = "before_tool_batch"
    BEFORE_TOOL_CALL = "before_tool_call"


@dataclass
class ControlRuntime:
    channel: ControlChannel
    store: ControlStore
    registry: CommandHandlerRegistry
    max_commands: int = 3

    async def drain(self, ctx: AgentContext[Any], *, phase: ControlPhase) -> None:
        from framework.control.types import ControlCommandType, ControlScope

        scope = ControlScope(session_id=ctx.session_id)
        commands = list(await self.channel.drain(
            scope, limit=self.max_commands,
            command_types={
                ControlCommandType.CANCEL_RUN,
                ControlCommandType.CANCEL_TURN,
                ControlCommandType.INJECT_USER_MESSAGE,
                ControlCommandType.SET_DYNAMIC_CONFIG,
            },
        ))
        if not commands:
            commands = await self.store.claim_commands(
                scope, limit=self.max_commands,
                command_types={
                    ControlCommandType.CANCEL_RUN,
                    ControlCommandType.CANCEL_TURN,
                    ControlCommandType.INJECT_USER_MESSAGE,
                    ControlCommandType.SET_DYNAMIC_CONFIG,
                },
            )

        for cmd in commands:
            handlers = self.registry.get(cmd.type)
            handled = False
            for handler in handlers:
                try:
                    handled = await handler.handle(ctx, cmd)
                    if handled:
                        break
                except Exception:
                    raise
            await self.store.mark_handled(cmd.command_id, {"handled": handled})
            if not handled:
                logger.debug(
                    "ControlRuntime: unhandled command type=%s session=%s",
                    cmd.type.value, ctx.session_id,
                )
