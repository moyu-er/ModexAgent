"""InjectionDrainer — consume the per-turn injection queue into history.

Extracted from ReActAgent._drain_injections so the LLMNode holds a collaborator
instead of a back-reference to the agent. Behaviour identical.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from modex_agent.core.agent import AgentContext
from modex_agent.runtime.enums import TurnCustomKey

logger = logging.getLogger(__name__)

_MAX_INJECTIONS_PER_PHASE = 3
_MAX_INJECTION_CYCLES = 5


class InjectionDrainer:
    """Drain queued user injections into history, with cycle + per-phase caps."""

    async def drain(
        self,
        ctx: AgentContext,
        max_per_phase: int = _MAX_INJECTIONS_PER_PHASE,
    ) -> list[str]:
        q = ctx.runtime.injection_queue if ctx.runtime else None
        if q is None:
            return []

        cycle_count: int = (
            ctx.runtime.state.custom.get(TurnCustomKey.INJECTION_CYCLE_COUNT, 0)
            if ctx.runtime
            else 0
        )
        if cycle_count >= _MAX_INJECTION_CYCLES:
            while True:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            return []

        injected: list[str] = []
        for _ in range(max_per_phase):
            try:
                msg: str = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await ctx.history.append(
                    {"role": "user", "content": f"[Injected during execution]: {msg}"}
                )
            except Exception:
                logger.warning(
                    "Failed to inject message into history, returning to queue: %s",
                    msg[:100],
                )
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(msg)
                break
            injected.append(msg)

        if injected and ctx.runtime:
            ctx.runtime.state.custom[TurnCustomKey.INJECTION_CYCLE_COUNT] = cycle_count + 1

        return injected
