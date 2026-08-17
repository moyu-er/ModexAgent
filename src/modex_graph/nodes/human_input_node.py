# ruff: noqa: ANN401
"""`HumanInputNode` + `HumanInputNodeFactory` — suspend for human input.

The node interrupts when no human answer is pending and otherwise passes
the most recently delivered answer downstream. Its behavior depends only on
`IntegratedInput`, so retries and crash recovery do not require instance state.

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
    """Suspends for human input via `GraphInterrupt`.

    With no pending payloads, `execute()` interrupts with the configured
    prompt. Otherwise, it delivers the last payload's content downstream.
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

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> None:
        """Interrupt without input; otherwise pass the latest answer through."""
        if integrated_input.payloads:
            answer = integrated_input.payloads[-1]
            self.deliver(answer.content, self._next_node, ctx)
        else:
            ctx.interrupt({"prompt": self._prompt, "node": self.name})
        return None


class HumanInputNodeFactory(NodeFactory):
    """Creates `HumanInputNode` from config.

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
