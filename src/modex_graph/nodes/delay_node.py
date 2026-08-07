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

from pydantic import BaseModel, ConfigDict

from ..integration import IntegratedInput
from ..node import Node
from ..node_factory import NodeFactory
from ..spec import NodeSpec

if TYPE_CHECKING:
    from ..context import GraphContext


class DelayNodeConfig(BaseModel):
    """Pydantic config schema for `DelayNode` (rule 12 — strict-shape).

    Fields:
    - `delay_seconds`: non-negative sleep duration. Pydantic coerces int/str
      to float; the `< 0` business check is in `DelayNode.__init__` and
      `DelayNodeFactory.create()`.
    - `next_node`: explicit deliver target (optional).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    delay_seconds: float = 0.0
    next_node: str | None = None


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
    ) -> None:
        """Sleep for the configured delay, then deliver a tick signal."""
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        self.deliver({"delayed_seconds": self._delay}, self._next_node, ctx)
        return None


class DelayNodeFactory(NodeFactory):
    """Creates `DelayNode` from config (ticket 02).

    `NodeSpec.config = {"delay_seconds": <float>, "next_node": <str> (optional)}`.

    Config shape is validated by `DelayNodeConfig` (returned from
    `config_schema()`). Pydantic coerces int/str to float; the `< 0`
    business check remains in `create()`.
    """

    def create(self, spec: NodeSpec) -> Node[Any]:
        """Create a `DelayNode` from the spec's `delay_seconds` config key.

        Config shape is validated via `DelayNodeConfig` — `delay_seconds` is
        guaranteed to be a `float` and `next_node` a `str | None`. The `< 0`
        business check remains (Pydantic validates type, not range).

        Raises:
            pydantic.ValidationError: if `spec.config` fails config validation.
            ValueError: if `delay_seconds` is negative.
        """
        config = DelayNodeConfig.model_validate(spec.config)
        if config.delay_seconds < 0:
            raise ValueError(
                f"DelayNode 'delay_seconds' must be >= 0 (got {config.delay_seconds})."
            )
        return DelayNode(config.delay_seconds, next_node=config.next_node)

    def config_schema(self) -> type[BaseModel]:
        """Return `DelayNodeConfig` — the Pydantic config model."""
        return DelayNodeConfig


__all__ = ["DelayNode", "DelayNodeConfig", "DelayNodeFactory"]
