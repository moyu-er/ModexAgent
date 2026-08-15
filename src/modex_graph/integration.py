# ruff: noqa: ANN401

"""`IntegratedPayload` + `IntegratedInput` + `InputIntegrator` — the
deliver/submit input integration layer (the deliver/submit design).

Provides:

- `IntegratedPayload` — frozen Pydantic value object: one upstream node's
  submitted payload, ready for downstream integration (rules 10-16).
- `IntegratedInput` — frozen Pydantic value object: the integrated result
  of all upstream payloads, fed to `Node.execute`.
- `InputIntegrator` ABC (rule 7: ABC, not Protocol) — the single seam for
  integrating multiple upstream `IntegratedPayload`s into one
  `IntegratedInput`.
- `DefaultInputIntegrator` — default impl: concatenates all payloads.
  `integrated_content = [p.content for p in payloads]`.

The deliver/submit model is the sole routing mechanism,
having fully replaced `transition`/`command`/`_compile_routing` (P3.4b
convergence — rule 15). Both `LinearScheduler` and `ParallelScheduler`
call `node.run()` which integrates upstream payloads via
`InputIntegrator`, calls `node.execute(ctx, integrated_input)`, then
`node._submit(ctx)` which dispatches grouped delivers via `ctx.dispatch`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .constants import DeliverConsumptionStatus


class GraphPayload(BaseModel):
    """Static-graph payload shared by graph input and output nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str


class IntegratedPayload(BaseModel):
    """One upstream node's submitted payload, ready for downstream integration.

    Frozen Pydantic value object (rules 10-16). Created by the framework
    when a node's `_submit` dispatches accumulated delivers to downstream
    nodes. Each `IntegratedPayload` represents one deliver entry from one
    upstream node.

    Fields:

    - `source_node: str` — the upstream node that submitted this payload.
    - `content: Any` — the delivered content (JSON-serializable).
    - `metadata: dict[str, Any]` — optional metadata (default empty).
    - `status: DeliverConsumptionStatus` — consumption status of the source
      deliver row. Business nodes use this to filter CONSUMED_PENDING rows on
      crash retry (ADR-0038 D5). STAGED rows are invisible to
      ``query_consumable`` so never appear here.
    - `consumed_by_invocation_id: int | None` — which invocation claimed
      this row (``None`` for fresh PENDING input). Business nodes distinguish
      "my own prior crashed attempt" from fresh input.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_node: str = Field(description="The upstream node that submitted this payload.")
    content: Any = Field(description="The delivered content (JSON-serializable).")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata.",
    )
    status: DeliverConsumptionStatus = Field(
        default=DeliverConsumptionStatus.PENDING,
        description="Consumption status of the source deliver row.",
    )
    consumed_by_invocation_id: int | None = Field(
        default=None,
        description="Which invocation claimed this row (None for fresh PENDING input).",
    )


class IntegratedInput(BaseModel):
    """The integrated result of all upstream payloads, fed to `Node.execute`.

    Frozen Pydantic value object (rules 10-16). Constructed by
    `InputIntegrator.integrate(payloads)`. Nodes access:

    - `integrated_content: Any` — the integrated content (default
      integrator concatenates as `list`). Nodes can interpret this freely.
    - `payloads: list[IntegratedPayload]` — the raw upstream payloads, for
      nodes that need custom integration (source-aware processing).

    Passed to `Node.execute(ctx, integrated_input)` as an explicit parameter.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    payloads: list[IntegratedPayload] = Field(
        default_factory=list,
        description="All upstream payloads, in submission order.",
    )
    integrated_content: Any = Field(
        default=None,
        description=(
            "The integrated content — default integrator concatenates as "
            "list. Nodes can access raw payloads for custom integration."
        ),
    )


class InputIntegrator(ABC):
    """Integrates multiple upstream `IntegratedPayload`s into one `IntegratedInput`.

    ABC (rule 7: ABC, not Protocol). The single seam for input integration
    logic. Subclasses override `integrate` to implement custom integration
    (e.g. merge by source, deduplicate, transform).

    The default impl `DefaultInputIntegrator` concatenates all payloads.
    """

    @abstractmethod
    def integrate(self, payloads: list[IntegratedPayload]) -> IntegratedInput:
        """Integrate `payloads` into a single `IntegratedInput`.

        Args:
            payloads: All upstream `IntegratedPayload`s, in submission order.

        Returns:
            An `IntegratedInput` with `payloads` preserved and
            `integrated_content` set per the integration strategy.
        """
        ...


class DefaultInputIntegrator(InputIntegrator):
    """Default `InputIntegrator`: concatenates all payloads.

    `integrated_content = [p.content for p in payloads]`. The raw
    `payloads` list is preserved on the returned `IntegratedInput` so
    nodes can access source-specific data.
    """

    def integrate(self, payloads: list[IntegratedPayload]) -> IntegratedInput:
        return IntegratedInput(
            payloads=payloads,
            integrated_content=[p.content for p in payloads],
        )


__all__ = [
    "GraphPayload",
    "IntegratedPayload",
    "IntegratedInput",
    "InputIntegrator",
    "DefaultInputIntegrator",
]
