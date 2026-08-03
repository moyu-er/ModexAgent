# ruff: noqa: ANN401
"""`FunctionNode` + `FunctionNodeFactory` — wrap a sync/async function as a Node.

Ticket 02 (P2.7): a generic node type that wraps a deterministic function
( sync or async ) into the `Node` ABC. The function receives the
`GraphContext`, returns a result, and `FunctionNode` delivers it to the next
node via `self.deliver(result, next_node, ctx)`.

The function signature is:

    def fn(ctx: GraphContext[Any]) -> Any
    async def fn(ctx: GraphContext[Any]) -> Any

Upstream payload data is available via the ``integrated_input`` parameter on
``execute``, same pattern as any other node.

The factory holds a `dict[str, Callable]` mapping registered names to
functions. `NodeSpec.config = {"function": "<name>", "next_node": "<target>"}`.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..integration import IntegratedInput
from ..node import Node
from ..node_factory import NodeFactory
from ..spec import NodeSpec

if TYPE_CHECKING:
    from ..context import GraphContext
    from ..result import NodeResult


class FunctionNode(Node[Any]):
    """Wraps a sync/async function as a `Node` (ticket 02).

    The function is called during `execute()`. Its return value is delivered
    to the next node via `self.deliver(result, next_node, ctx)`.

    If the function returns an awaitable (i.e. it is `async def`), the node
    awaits it before delivering — mirroring the dual-mode design of
    `Node.execute` (ADR-0033 D2).
    """

    def __init__(
        self,
        func: Callable[..., Any],
        *,
        next_node: str | None = None,
    ) -> None:
        """Initialize the function wrapper.

        Args:
            func: the sync or async function to wrap. Signature:
                `fn(ctx: GraphContext[Any]) -> Any` or
                `async def fn(ctx: GraphContext[Any]) -> Any`.
            next_node: the explicit deliver target. If `None`, the
                `_submit` step raises `NotImplementedError` (additive
                limitation — topology resolution lands at convergence).
        """
        self._func = func
        self._next_node = next_node

    async def execute(
        self,
        ctx: GraphContext[Any],
        integrated_input: IntegratedInput,
    ) -> NodeResult:
        """Call the wrapped function and deliver its result."""
        from ..result import NodeResult

        result = self._func(ctx)
        if inspect.isawaitable(result):
            result = await result
        self.deliver(result, self._next_node, ctx)
        return NodeResult()


class FunctionNodeFactory(NodeFactory):
    """Creates `FunctionNode` from a function registry (ticket 02).

    The factory holds a `dict[str, Callable]` mapping names to functions.
    `NodeSpec.config = {"function": "<registered_name>",
    "next_node": "<target>" (optional)}`.

    The factory does NOT declare a `config_schema` (returns `None`) — the
    function name is validated at `create()` time against the registry.
    """

    def __init__(self, functions: dict[str, Callable[..., Any]] | None = None) -> None:
        """Initialize with an optional pre-populated function registry.

        Args:
            functions: initial name→function mapping. May be `None` (empty);
                use `register_function` to add entries after construction.
        """
        self._functions: dict[str, Callable[..., Any]] = (
            dict(functions) if functions is not None else {}
        )

    def register_function(self, name: str, func: Callable[..., Any]) -> None:
        """Register a function under `name`.

        Raises:
            ValueError: if `name` is already registered (no silent override).
        """
        if name in self._functions:
            raise ValueError(
                f"Function {name!r} is already registered. "
                f"Use a different name or unregister first."
            )
        self._functions[name] = func

    def unregister_function(self, name: str) -> None:
        """Remove `name` from the function registry. No-op if not registered."""
        self._functions.pop(name, None)

    def create(self, spec: NodeSpec) -> Node[Any]:
        """Create a `FunctionNode` from the spec's `function` config key.

        Raises:
            ValueError: if `config["function"]` is missing, not a string,
                or not a registered function name.
        """
        func_name = spec.config.get("function")
        if not func_name or not isinstance(func_name, str):
            raise ValueError(
                f"FunctionNode requires a 'function' config key (string). "
                f"Got: {func_name!r}. Spec: {spec!r}."
            )
        func = self._functions.get(func_name)
        if func is None:
            raise ValueError(
                f"Function {func_name!r} is not registered. "
                f"Registered functions: {sorted(self._functions.keys())}."
            )
        next_node = spec.config.get("next_node")
        if next_node is not None and not isinstance(next_node, str):
            raise ValueError(
                f"FunctionNode 'next_node' config must be a string or None. Got: {next_node!r}."
            )
        return FunctionNode(func, next_node=next_node)

    def config_schema(self) -> type[BaseModel] | None:
        """No Pydantic schema — config is validated in `create()`."""
        return None


__all__ = ["FunctionNode", "FunctionNodeFactory"]
