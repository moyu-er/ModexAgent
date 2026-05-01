"""Graph interrupt mechanism — interrupt() + GraphInterrupt + _current_resume.

Used by SuspendStrategy to pause graph execution and resume with injected values.
"""

from __future__ import annotations

import contextvars
from typing import Any


class GraphInterrupt(Exception):
    """Graph execution interrupt raised by interrupt().

    Carries the value the node wanted to pass upward (e.g. approval requests),
    plus optional metadata for the engine/pipeline to use when resuming.
    """

    value: Any
    node_name: str
    iteration: int

    def __init__(
        self,
        value: Any,
        node_name: str = "",
        iteration: int = 0,
    ) -> None:
        super().__init__(str(value))
        self.value = value
        self.node_name = node_name
        self.iteration = iteration


_current_resume: contextvars.ContextVar[Any] = contextvars.ContextVar("_gr_resume")


def interrupt(value: Any) -> Any:
    """Interrupt graph execution with a value.

    First call (no resume context) raises GraphInterrupt.
    Second call (after resume context is set) returns the injected resume value.
    """
    resume = _current_resume.get(None)
    if resume is not None:
        return resume
    raise GraphInterrupt(value=value)
