"""Control drain utility — shared function for safe-point channel consumption.

All consumers (hooks at safe points, interceptors wrapping long operations)
call `drain_control_channel()` to atomically drain and validate control
commands from InMemoryControlChannel.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modex_agent.control.types import ControlCommandType, ControlScope

if TYPE_CHECKING:
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.core.agent import AgentContext

from collections.abc import AsyncIterator

from modex_agent.core.tool_manager import ToolResult
from modex_agent.interceptor.abc import (
    LLMStreamChunk,
    LLMStreamContext,
    LLMStreamInterceptor,
    LLMStreamNext,
    ToolCallContext,
    ToolCallInterceptor,
    ToolCallNext,
)

logger = logging.getLogger(__name__)


async def drain_control_channel(
    channel: InMemoryControlChannel | None,
    ctx: AgentContext,
    command_types: set[ControlCommandType] | None = None,
    *,
    turn_uuid: str | None = None,
) -> bool:
    """Drain control commands for the current session and execute actions.

    Drains ALL matching commands at once from the channel (atomic, destructive).
    For each command:
    - If command's turn_uuid matches → execute action (e.g., raise AgentCancelled)
    - If command's turn_uuid does NOT match → consume (discard) silently
    - If no turn_uuid in command payload → execute (backward-compatible defense)

    Args:
        channel: The InMemoryControlChannel to drain from.
        ctx: Current agent context (provides session_id).
        command_types: Types to drain. Defaults to {CANCEL_TURN}.
        turn_uuid: Current turn UUID for stale command validation.

    Returns:
        True if a command was consumed (matched or discarded).
        False if no commands were found.

    Raises:
        AgentCancelled: When a CANCEL_TURN command matches the current turn.
    """
    if channel is None:
        return False

    if command_types is None:
        command_types = {ControlCommandType.CANCEL_TURN}

    # ctx.session always has {snowflake}.{agent_name} format, so str() is
    # already canonical — no normalization needed.
    canonical_sid = str(ctx.session)
    scope = ControlScope(session_id=canonical_sid)
    cmds = await channel.drain(scope, limit=0, command_types=command_types)

    if not cmds:
        return False

    for cmd in cmds:
        cmd_turn_uuid = cmd.payload.get("turn_uuid") if cmd.payload else None
        cmd_type_name = cmd.type.value if hasattr(cmd.type, "value") else str(cmd.type)

        # Stale command: turn_uuid mismatch
        if turn_uuid is not None and cmd_turn_uuid is not None:
            if cmd_turn_uuid != turn_uuid:
                logger.debug(
                    "Control: discarding stale %s cmd_uuid=%s current_uuid=%s",
                    cmd_type_name,
                    cmd_turn_uuid,
                    turn_uuid,
                )
                continue

        # Execute matching command
        if cmd.type == ControlCommandType.CANCEL_TURN:
            logger.info(
                "Control: executing CANCEL_TURN session=%s turn_uuid=%s",
                str(ctx.session),
                turn_uuid,
            )
            from modex_agent.control.exceptions import AgentCancelled

            raise AgentCancelled("User requested /stop")

    return True


class ControlDrainInterceptor(ToolCallInterceptor):
    """Drains control channel before each tool call.

    If CANCEL_TURN matches, AgentCancelled propagates through the
    interceptor chain, causing the ReAct graph to exit via AgentControlError
    propagation (InterceptorChain.around_tool_call re-raises AgentControlError).
    """

    def __init__(self, channel) -> None:
        self._channel = channel

    @property
    def name(self) -> str:
        return "control_drain_tool"

    async def around_tool_call(
        self,
        ctx,
        call: ToolCallContext,
        next_call: ToolCallNext,
    ) -> ToolResult:
        await drain_control_channel(
            self._channel,
            ctx,
            turn_uuid=ctx.current_turn_uuid,
        )
        return await next_call()


class LlmCancelInterceptor(LLMStreamInterceptor):
    """Drains control channel during LLM streaming.

    Checks for CANCEL_TURN before each chunk. If a matching CANCEL_TURN
    is found, AgentCancelled propagates immediately — aborting the stream
    and causing the ReAct graph to exit via AgentControlError propagation.
    This is a "hard cancel": the command is consumed destructively and the
    exception prevents any subsequent tool calls from executing.

    Contrast with the 4 safe points (which also trigger hard cancel) —
    this interceptor covers the LLM streaming window specifically because
    a long-running streaming call would otherwise block a safe point from
    being reached.
    """

    def __init__(self, channel) -> None:
        self._channel = channel

    @property
    def name(self) -> str:
        return "llm_cancel"

    async def around_llm_stream(
        self,
        ctx,
        call: LLMStreamContext,
        next_stream: LLMStreamNext,
    ) -> AsyncIterator[LLMStreamChunk]:
        async for chunk in next_stream():
            await drain_control_channel(
                self._channel,
                ctx,
                turn_uuid=ctx.current_turn_uuid,
            )
            yield chunk
