# ruff: noqa: ANN401
"""`HumanInputNode` + `HumanInputNodeFactory` — suspend for human input.

Ticket 02 (P2.10): a generic node that suspends graph execution for human
input via `GraphInterrupt`. On first entry, `execute()` raises
`GraphInterrupt` with a prompt payload. On resume (re-entry), the node
delivers a "human_input_resumed" signal.

Suspend-without-re-execution model (ADR-0033 D7): already-applied state
updates persist across the interrupt boundary. Resume re-enters the graph
at the entry node; the interrupted node body is NOT re-run in the real
engine flow. This node uses a `_resumed` phase flag to distinguish first
entry from resume when `execute` IS called again (e.g. in isolated testing
or when the convergence step wires re-entry semantics).

`NodeSpec.config = {"prompt": <str>, "next_node": <str> (optional)}`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict

from ..integration import IntegratedInput
from ..node import Node
from ..node_factory import NodeFactory
from ..spec import NodeSpec

if TYPE_CHECKING:
    from ..context import GraphContext
    from ..result import NodeResult


class HumanInputNodeConfig(BaseModel):
    """Pydantic config schema for `HumanInputNode` (rule 12 — strict-shape).

    Fields:
    - `prompt`: the prompt displayed to the human when the graph suspends.
    - `next_node`: explicit deliver target for the resume signal (optional).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = ""
    next_node: str | None = None


class HumanInputNode(Node[Any]):
    """Suspends for human input via `GraphInterrupt` (ticket 02).

    `execute()`:

    - First entry (`_resumed` is False): sets `_resumed = True`, then calls
      `ctx.interrupt({"prompt": ..., "node": ...})` which raises
      `GraphInterrupt`. The lines after the interrupt call are unreachable
      on first entry.
    - Resume (`_resumed` is True): skips the interrupt, delivers
      `{"human_input": "resumed", "prompt": ...}` to the next node, and
      resets `_resumed` to False for potential re-use.

    In the real engine flow (suspend-without-re-execution), the node body
    is NOT re-run on resume — the graph re-enters at the entry node. The
    `_resumed` flag is the testing seam: a test simulates resume by setting
    `_resumed = True` before calling `_execute`, verifying the deliver path.
    """

    def __init__(self, prompt: str, *, next_node: str | None = None) -> None:
        """Initialize the human-input node.

        Args:
            prompt: the prompt displayed to the human when the graph
                suspends. Carried in the `GraphInterrupt` payload.
            next_node: the explicit deliver target for the resume signal.
        """
        self._prompt = prompt
        self._next_node = next_node
        self._resumed = False

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> NodeResult:
        """Suspend for human input on first entry; deliver on resume."""
        from ..result import NodeResult

        if not self._resumed:
            self._resumed = True
            ctx.interrupt({"prompt": self._prompt, "node": self.name})
        # On resume (re-entry), _resumed is True — deliver the resume signal.
        self.deliver(
            {"human_input": "resumed", "prompt": self._prompt},
            self._next_node,
            ctx,
        )
        self._resumed = False
        return NodeResult()


class HumanInputNodeFactory(NodeFactory):
    """Creates `HumanInputNode` from config (ticket 02).

    `NodeSpec.config = {"prompt": <str>, "next_node": <str> (optional)}`.

    Config shape is validated by `HumanInputNodeConfig` (returned from
    `config_schema()`).
    """

    def create(self, spec: NodeSpec) -> Node[Any]:
        """Create a `HumanInputNode` from the spec's `prompt` config key.

        Config shape is validated via `HumanInputNodeConfig` — `prompt` is
        guaranteed to be a `str` and `next_node` a `str | None`.

        Raises:
            pydantic.ValidationError: if `spec.config` fails config validation.
        """
        config = HumanInputNodeConfig.model_validate(spec.config)
        return HumanInputNode(config.prompt, next_node=config.next_node)

    def config_schema(self) -> type[BaseModel]:
        """Return `HumanInputNodeConfig` — the Pydantic config model."""
        return HumanInputNodeConfig


__all__ = ["HumanInputNode", "HumanInputNodeConfig", "HumanInputNodeFactory"]
