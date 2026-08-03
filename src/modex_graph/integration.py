# ruff: noqa: ANN401

"""`IntegratedPayload` + `IntegratedInput` + `InputIntegrator` — the
deliver/submit input integration layer (ticket 07).

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

Per ticket 07: the deliver/submit model replaces `transition`/`command`/
`_compile_routing` conceptually. This step is ADDITIVE — the old mechanisms
stay; the new `_execute`/`_deliver`/`_submit` methods on `Node` are
testable in isolation and NOT yet wired into the scheduler loop. Wiring +
old-mechanism removal happens in a later convergence step.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_node: str = Field(description="The upstream node that submitted this payload.")
    content: Any = Field(description="The delivered content (JSON-serializable).")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional metadata.",
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
    "IntegratedPayload",
    "IntegratedInput",
    "InputIntegrator",
    "DefaultInputIntegrator",
]
