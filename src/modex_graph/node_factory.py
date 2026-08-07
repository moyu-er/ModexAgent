# ruff: noqa: ANN401
"""`NodeFactory` ABC + `NodeRegistry` — declarative node construction.

Per ticket 08: a `NodeFactory` creates `Node` instances from a `NodeSpec`'s
config. Factories are registered by `node_type` string (e.g. `"function"`,
`"agent"`, `"delay"`); the registry looks up the factory and validates the
spec's `config` dict against an optional Pydantic schema declared at
registration time.

This is the declarative construction path — the alternative to the
imperative `Graph.add_node(name, node_instance)`. Business modules register
factories for their node types (for example, function and agent nodes)
and then construct graphs entirely from `GraphSpec` data.

Architecture:
- `NodeFactory` is an ABC (rule 7 — ABCs, not Protocols). Subclasses
  implement `create(spec)` and `config_schema()`.
- `NodeRegistry` is a regular class holding `dict[node_type, (factory, schema)]`.
- The registry is the only place where `node_type` strings are resolved to
  concrete factories. This is the seam for future extension (e.g. plugin
  discovery).

Generic Node implementations (`FunctionNode`, `AgentNode`, etc.) are P2
(ticket 09). They will register their own factories here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel

from .node import Node
from .spec import NodeSpec


class NodeFactory(ABC):
    """Creates `Node` instances from a `NodeSpec`. Registered by `node_type`.

    Subclasses implement two methods:
    - `create(spec) -> Node[Any]` — construct the `Node` from the spec's
      `config` dict (already validated against `config_schema()`).
    - `config_schema() -> type[BaseModel] | None` — return the Pydantic
      model validating `NodeSpec.config`, or `None` if no validation is
      needed.

    The factory is registered with a `NodeRegistry` under a `node_type`
    string. At compile time, `GraphSpecCompiler` (P2) calls
    `registry.create(spec)` for each `NodeSpec` in a `GraphSpec`.
    """

    @abstractmethod
    def create(self, spec: NodeSpec) -> Node[Any]:
        """Construct a `Node` from the validated `spec.config`.

        Args:
            spec: the `NodeSpec` with `config` already validated against
                `config_schema()` by the registry.

        Returns:
            A `Node` instance. The registry sets `node.name = spec.name`
            after construction (matching `Graph.add_node` behavior).
        """
        ...

    @abstractmethod
    def config_schema(self) -> type[BaseModel] | None:
        """Return the Pydantic model validating `NodeSpec.config`, or `None`.

        Returning `None` means the factory accepts any config dict (no
        validation). Returning a `BaseModel` subclass means the registry
        will validate `spec.config` against it before calling `create()`.
        """
        ...


class NodeRegistry:
    """Registry of `NodeFactory` by `node_type` string.

    Usage:

    ```python
    registry = NodeRegistry()
    registry.register("function", FunctionFactory(), FunctionConfig)
    registry.register("delay", DelayFactory())  # no config schema

    node = registry.create(NodeSpec(name="n1", node_type="function", config={...}))
    ```

    The registry is the single seam between declarative specs and concrete
    `Node` instances. Business modules register their factories here at
    startup; `GraphSpecCompiler` (P2) uses the registry to materialize a
    `GraphSpec` into a `Graph`.
    """

    def __init__(self) -> None:
        # factory + optional config-validation model, keyed by node_type
        self._factories: dict[str, tuple[NodeFactory, type[BaseModel] | None]] = {}

    def register(
        self,
        node_type: str,
        factory: NodeFactory,
        config_model: type[BaseModel] | None = None,
    ) -> None:
        """Register `factory` under `node_type`.

        Args:
            node_type: the string key matching `NodeSpec.node_type`.
            factory: the `NodeFactory` implementation.
            config_model: optional Pydantic model validating `NodeSpec.config`
                before `factory.create()` is called. If `None`, the factory's
                own `config_schema()` is called to obtain the model. Pass
                `None` explicitly AND have `config_schema()` return `None` to
                skip validation entirely.

        Raises:
            ValueError: if `node_type` is already registered (no silent
                override — re-registering is a bug smell).
        """
        if node_type in self._factories:
            raise ValueError(
                f"Node type {node_type!r} is already registered. "
                f"Use a different name or unregister first."
            )
        # If no config_model passed, ask the factory for its schema.
        resolved_model = config_model if config_model is not None else factory.config_schema()
        self._factories[node_type] = (factory, resolved_model)

    def unregister(self, node_type: str) -> None:
        """Remove `node_type` from the registry. No-op if not registered."""
        self._factories.pop(node_type, None)

    def create(self, spec: NodeSpec) -> Node[Any]:
        """Look up the factory for `spec.node_type`, validate config, create Node.

        Args:
            spec: the `NodeSpec` describing the node to create.

        Returns:
            A `Node` instance with `name` set to `spec.name`.

        Raises:
            KeyError: if `spec.node_type` is not registered.
            pydantic.ValidationError: if `spec.config` fails validation
                against the factory's `config_schema()`.
        """
        entry = self._factories.get(spec.node_type)
        if entry is None:
            raise KeyError(
                f"Node type {spec.node_type!r} is not registered. "
                f"Registered types: {sorted(self._factories.keys())}."
            )
        factory, config_model = entry
        # Validate config against the declared schema (if any).
        # The validated model is NOT passed to factory.create() — factories
        # read from spec.config. Validation is a side-check that raises
        # ValidationError before create() is called. This keeps the factory
        # interface simple (it takes NodeSpec, not a typed config object).
        if config_model is not None:
            config_model.model_validate(spec.config)
        node = factory.create(spec)
        node.name = spec.name
        if spec.trigger is not None:
            node.trigger = spec.trigger
        return node

    def is_registered(self, node_type: str) -> bool:
        """True if `node_type` has a registered factory."""
        return node_type in self._factories

    def registered_types(self) -> list[str]:
        """Sorted list of registered `node_type` strings."""
        return sorted(self._factories.keys())


__all__ = ["NodeFactory", "NodeRegistry"]
