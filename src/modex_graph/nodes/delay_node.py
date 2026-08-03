# ruff: noqa: ANN401
"""`DelayNode` + `DelayNodeFactory` — async delay / rate-limiting node.

Ticket 02 (P2.9): a generic node that sleeps for a configured duration,
then delivers a tick signal. Useful for rate limiting, polling intervals,
or pacing between nodes.

`NodeSpec.config = {"delay_seconds": <float>, "next_node": <str> (optional)}`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..integration import IntegratedInput
from ..node import Node
from ..node_factory import NodeFactory
from ..spec import NodeSpec

if TYPE_CHECKING:
    from ..context import GraphContext
    from ..result import NodeResult


class DelayNode(Node[Any]):
    """Async delay / rate-limiting node (ticket 02).

    `execute()` sleeps for the configured `delay_seconds`, then delivers
    `{"delayed_seconds": <float>}` to the next node. The sleep uses
    `asyncio.sleep` — non-blocking under both `LinearScheduler` and
    `ParallelScheduler`.
    """

    def __init__(self, delay_seconds: float, *, next_node: str | None = None) -> None:
        """Initialize the delay node.

        Args:
            delay_seconds: the duration to sleep before delivering. A
                non-negative float. Values <= 0 deliver immediately (no
                sleep) — useful for testing.
            next_node: the explicit deliver target.
        """
        if delay_seconds < 0:
            raise ValueError(f"delay_seconds must be >= 0 (got {delay_seconds}).")
        self._delay = delay_seconds
        self._next_node = next_node

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> NodeResult:
        """Sleep for the configured delay, then deliver a tick signal."""
        from ..result import NodeResult

        if self._delay > 0:
            await asyncio.sleep(self._delay)
        self.deliver({"delayed_seconds": self._delay}, self._next_node, ctx)
        return NodeResult()


class DelayNodeFactory(NodeFactory):
    """Creates `DelayNode` from config (ticket 02).

    `NodeSpec.config = {"delay_seconds": <float>, "next_node": <str> (optional)}`.
    """

    def create(self, spec: NodeSpec) -> Node[Any]:
        """Create a `DelayNode` from the spec's `delay_seconds` config key.

        Raises:
            ValueError: if `delay_seconds` is present but not coercible to
                a non-negative float, or is negative.
        """
        delay = spec.config.get("delay_seconds", 0.0)
        try:
            delay_float = float(delay)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"DelayNode 'delay_seconds' must be a number. Got: {delay!r}."
            ) from exc
        if delay_float < 0:
            raise ValueError(f"DelayNode 'delay_seconds' must be >= 0 (got {delay_float}).")
        next_node = spec.config.get("next_node")
        if next_node is not None and not isinstance(next_node, str):
            raise ValueError(
                f"DelayNode 'next_node' config must be a string or None. Got: {next_node!r}."
            )
        return DelayNode(delay_float, next_node=next_node)

    def config_schema(self) -> type[BaseModel] | None:
        """No Pydantic schema — config is validated in `create()`."""
        return None


__all__ = ["DelayNode", "DelayNodeFactory"]
