"""CompositeEmitter — fan-out sink delegating to a list of children.

A thin bot subclass of the framework
:class:`~modex_agent.core.emitter.CompositeTurnEventSink`: delivery order
and failure isolation live on the framework composite; the bot layer only
adds the ``set_sessions_dir_provider`` forwarding so the per-workspace
transcript resolver reaches every channel child.

Usage::

    emitter = CompositeEmitter([
        WebBotEmitter(ws_output, session_id, ...),
        QQBotEmitter(qq_output, session_id, ...),
    ])
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from modex_agent.core.emitter import CompositeTurnEventSink

logger = logging.getLogger(__name__)


class CompositeEmitter(CompositeTurnEventSink):
    """Fan-out sink that delegates every event to a list of child sinks.

    Every gate-passing event is delivered to every child IN ORDER; one
    child's delivery failure is logged and does not starve the remaining
    children (framework composite semantics).

    Usage::

        emitter = CompositeEmitter([
            WebBotEmitter(ws_output, session_id, ...),
            QQBotEmitter(qq_output, session_id, ...),
        ])
    """

    def set_sessions_dir_provider(
        self, provider: Callable[[], Path | None] | None
    ) -> None:
        """Forward the sessions_dir provider to every child sink that accepts one."""
        for child in self.children:
            setter = getattr(child, "set_sessions_dir_provider", None)
            if setter is not None:
                setter(provider)
