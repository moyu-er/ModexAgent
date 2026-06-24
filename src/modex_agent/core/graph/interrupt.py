"""Graph interrupt mechanism: ``interrupt()`` + ``GraphInterrupt``.

Agent nodes use this to pause graph execution after persisting resumable turn
state through the runtime turn store.
"""

from __future__ import annotations

from typing import Any


class GraphInterrupt(Exception):  # noqa: N818
    """Graph execution interrupt raised by interrupt()."""

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


def interrupt(value: Any) -> Any:
    """Interrupt graph execution by raising ``GraphInterrupt``."""
    raise GraphInterrupt(value=value)
