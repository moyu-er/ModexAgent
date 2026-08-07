"""CompositeEmitter — fan-out emitter that delegates to a list of children.

Each method is forwarded to ALL children concurrently via
``asyncio.gather(..., return_exceptions=True)``.  Errors in individual
children are logged but do not prevent other children from receiving the
event.

Usage::

    emitter = CompositeEmitter([
        WebBotEmitter(ws_output, session_id, ...),
        QQBotEmitter(qq_output, session_id, ...),
    ])
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, TypeVar

from modex_agent.core.emitter import AgentResult, ContentEmitter, EmitterConfig
from modex_agent.core.turn_events import TurnEvent

logger = logging.getLogger(__name__)

_E = TypeVar("_E")


class CompositeEmitter(ContentEmitter[_E], Generic[_E]):
    """Fan-out emitter that delegates all calls to a list of child emitters.

    Each method is forwarded to ALL children concurrently via
    ``asyncio.gather(..., return_exceptions=True)``.  Errors in individual
    children are logged but do not prevent other children from receiving the
    event.

    Usage::

        emitter = CompositeEmitter([
            WebBotEmitter(ws_output, session_id, ...),
            QQBotEmitter(qq_output, session_id, ...),
        ])
    """

    def __init__(
        self,
        emitters: list[ContentEmitter[_E]],
        config: EmitterConfig | None = None,
    ) -> None:
        super().__init__(config)
        self._emitters: list[ContentEmitter[_E]] = list(emitters)

    @property
    def emitters(self) -> list[ContentEmitter[_E]]:
        return list(self._emitters)

    def set_sessions_dir_provider(
        self, provider: Callable[[], Path | None] | None
    ) -> None:
        """Forward the sessions_dir provider to every child emitter that accepts one."""
        for child in self._emitters:
            setter = getattr(child, "set_sessions_dir_provider", None)
            if setter is not None:
                setter(provider)

    def wants_streaming(self) -> bool:
        """Return ``True`` if ANY child wants streaming."""
        return any(e.wants_streaming() for e in self._emitters)

    async def emit(self, event: _E, data: Any = None) -> None:
        results = await asyncio.gather(
            *(e.emit(event, data) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit")

    async def emit_delta(self, delta: str) -> None:
        results = await asyncio.gather(
            *(e.emit_delta(delta) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_delta")

    async def emit_turn_event(self, event: TurnEvent) -> None:
        results = await asyncio.gather(
            *(e.emit_turn_event(event) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_turn_event")

    async def emit_content(self, full_content: str) -> None:
        results = await asyncio.gather(
            *(e.emit_content(full_content) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_content")

    async def emit_stream_end(self, resuming: bool = False) -> None:
        results = await asyncio.gather(
            *(e.emit_stream_end(resuming) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_stream_end")

    async def emit_complete(self, result: AgentResult) -> None:
        results = await asyncio.gather(
            *(e.emit_complete(result) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_complete")

    async def emit_error(self, error: str) -> None:
        results = await asyncio.gather(
            *(e.emit_error(error) for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "emit_error")

    async def flush(self) -> None:
        results = await asyncio.gather(
            *(e.flush() for e in self._emitters),
            return_exceptions=True,
        )
        self._log_exceptions(results, "flush")

    @staticmethod
    def _log_exceptions(results: list[object], method: str) -> None:
        """Log any exceptions from ``asyncio.gather(return_exceptions=True)``."""
        for exc in results:
            if isinstance(exc, Exception):
                logger.error("CompositeEmitter.%s child error: %s", method, exc)
