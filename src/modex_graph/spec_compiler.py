# ruff: noqa: ANN401
"""`GraphSpecCompiler` — bridge between declarative `GraphSpec` and `CompiledGraph`.

Per ticket 08: the full chain is

    GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance → GraphEngine

`GraphSpecCompiler` resolves `StateFactory` / `NodeFactory` from registries,
builds a `Graph` topology, runs `TopologyValidator`, and calls
`graph.compile()` to produce a `CompiledGraph`.

State is NOT created here — that happens at `GraphInstance` instantiation
time (P3.5 bot factory), where `StateFactory.create_state()` is called to
produce the runtime state. The compiler only validates that the
`StateFactory` CAN create the state (resolves the schema / registered name
and constructs the factory, which may eagerly build the dynamic state class).

Type parameter note: the compiler does not know the state type `S` at
compile time. `StateFactory` may be a `DynamicStateFactory` that builds a
`GraphState` subclass at runtime via `pydantic.create_model`. The returned
`CompiledGraph` is therefore typed `CompiledGraph[Any]` — the one place
`Any` is justified in this package (state type is runtime-determined).
"""

from __future__ import annotations

from typing import Any

from .compiled_graph import CompiledGraph
from .graph import Graph
from .node_factory import NodeRegistry
from .spec import GraphSpec
from .state_factory import DynamicStateFactory, StateFactory, StateRegistry
from .state_schema import StateSchema
from .topology_validator import TopologyValidator

# Default validator instance, lazily constructed. Reused across compile()
# calls when no custom validator is injected. TopologyValidator is stateless
# so sharing one instance is safe.
_DEFAULT_VALIDATOR: TopologyValidator | None = None


def _default_validator() -> TopologyValidator:
    """Return the shared default `TopologyValidator` instance."""
    global _DEFAULT_VALIDATOR
    if _DEFAULT_VALIDATOR is None:
        _DEFAULT_VALIDATOR = TopologyValidator()
    return _DEFAULT_VALIDATOR


class GraphSpecCompiler:
    """Compiles `GraphSpec` → `CompiledGraph` (ticket 08).

    Full chain: `GraphSpec → GraphSpecCompiler → CompiledGraph →
    GraphInstance → GraphEngine`.

    Steps:

    1. Resolve `StateFactory` from `spec.state_schema` (inline `StateSchema`
       → `DynamicStateFactory`, registered name → `StateRegistry`). The
       factory is resolved for validation only — no state is created.
    2. Build `Graph` topology (create `Node` instances via `NodeRegistry`,
       `add_node`, `add_edge` — edges are plain topology).
    3. Run `TopologyValidator` on the spec.
    4. Call `graph.compile(max_iterations, scheduler, default_trigger)` →
       `CompiledGraph`.

    Does NOT create state (state is at `GraphInstance` level, created by
    `StateFactory.create_state()` later).

    Usage:

    ```python
    compiler = GraphSpecCompiler(node_registry, state_registry)
    compiled: CompiledGraph[Any] = compiler.compile(spec)
    ```
    """

    def __init__(
        self,
        node_registry: NodeRegistry,
        state_registry: StateRegistry,
        validator: TopologyValidator | None = None,
    ) -> None:
        """Initialize the compiler with the required registries.

        Args:
            node_registry: registry of `NodeFactory` by `node_type` string.
                Used to materialize `NodeSpec` → `Node` instances.
            state_registry: registry of `StateFactory` by name. Used to
                resolve `GraphSpec.state_schema` when it is a registered
                name (string).
            validator: optional `TopologyValidator` override. If `None`,
                a shared default instance is used. Injecting a custom
                validator is primarily for tests.
        """
        self._node_registry = node_registry
        self._state_registry = state_registry
        self._validator = validator

    def compile(self, spec: GraphSpec) -> CompiledGraph[Any]:
        """Compile a `GraphSpec` into a `CompiledGraph`.

        Raises:
            TopologyError: if topology validation fails.
            KeyError: if a `NodeSpec.node_type` is not registered in the
                `NodeRegistry`.
            ValueError: if a registered `StateFactory` name is not found in
                the `StateRegistry`, or if `DynamicStateFactory` construction
                fails on a bad inline `StateSchema`.
            pydantic.ValidationError: if a `NodeSpec.config` fails
                validation against the factory's `config_schema()`.
            RoutingError: if `Graph.compile()` finds a structural issue that
                `TopologyValidator` did not catch (should not happen in
                practice — the validator is stricter).
        """
        # 1. Resolve StateFactory (validates state_schema — no state created).
        # The factory is not stored on the CompiledGraph; resolution IS the
        # validation. Discarding the bound name is intentional.
        self._resolve_state_factory(spec.state_schema)

        # 2. Build Graph topology. State type S is runtime-determined (may
        # be a DynamicStateFactory-built class), so the Graph is typed Any.
        graph: Graph[Any] = Graph(name=spec.name)

        # 3. Create and register nodes from NodeSpecs.
        for node_spec in spec.nodes:
            node = self._node_registry.create(node_spec)
            graph.add_node(node_spec.name, node)

        # 4. Add edges. EdgeSpec is topology-only (deliver/submit model).
        for edge in spec.edges:
            graph.add_edge(edge.source, edge.target)

        # 5. Validate topology (runs BEFORE graph.compile() to fail fast
        # on topology issues before the Graph builder's own checks).
        validator = self._validator if self._validator is not None else _default_validator()
        validator.validate(spec)

        # 6. Compile. default_trigger is passed through from the spec so
        # PARALLEL scheduler graphs respect the spec's trigger default.
        # cycle_detection stays at "warn" (default) — TopologyValidator
        # already allows cycles; Graph.compile's warn is consistent.
        compiled = graph.compile(
            max_iterations=spec.max_iterations,
            scheduler=spec.scheduler,
            default_trigger=spec.default_trigger,
        )
        return compiled

    def _resolve_state_factory(self, state_schema: StateSchema | str) -> StateFactory:
        """Resolve `StateFactory` from an inline `StateSchema` or registered name.

        - Inline (`StateSchema` instance): construct a `DynamicStateFactory`.
          The factory builds the `GraphState` subclass eagerly in `__init__`,
          so construction IS the validation — bad schemas (unresolvable
          types, bad channels) raise here.
        - Registered (`str`): look up the factory in `StateRegistry`. Raises
          `ValueError` if the name is not registered.

        The returned factory is NOT used to create state (that happens at
        `GraphInstance` instantiation). Resolution is validation only.
        """
        if isinstance(state_schema, StateSchema):
            # DynamicStateFactory.__init__ builds the state class eagerly —
            # raises ValueError on unresolvable field types / bad channels.
            return DynamicStateFactory(state_schema)
        # Registered name — verify existence and retrieve the factory.
        factory = self._state_registry.get_factory(state_schema)
        if factory is None:
            raise ValueError(
                f"StateFactory {state_schema!r} is not registered. "
                f"Registered names: {self._state_registry.registered_names()}."
            )
        return factory


__all__ = ["GraphSpecCompiler"]
