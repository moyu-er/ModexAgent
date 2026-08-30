# ruff: noqa: ANN401
"""`GraphSpecCompiler` — bridge between declarative `GraphSpec` and `CompiledGraph`.

The full chain is

    GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance → GraphEngine

`GraphSpecCompiler` resolves state classes and node factories,
builds a `Graph` topology, runs `TopologyValidator`, and calls
`graph.compile()` to produce a `CompiledGraph`.

State is not created here. The compiler validates that `spec.state_class`
names an injected `GraphState` subclass (or that `spec.state_schema` is
resolved by the injected `state_schema_compiler`); the orchestrator creates
runtime state at `GraphInstance` construction.

The returned `CompiledGraph` is typed `CompiledGraph[Any]` because the state
class is selected from a runtime mapping.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .compiled_graph import CompiledGraph
from .constants import GraphNode
from .graph import Graph
from .node_factory import NodeRegistry
from .spec import FieldSpec, GraphSpec, NodeSpec
from .state import GraphState
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
    """Compiles `GraphSpec` → `CompiledGraph`.

    Full chain: `GraphSpec → GraphSpecCompiler → CompiledGraph →
    GraphInstance → GraphEngine`.

    Steps:

    1. Resolve state: either `spec.state_class` from the injected
       state-class mapping, OR `spec.state_schema` via the injected
       `state_schema_compiler` (SPEC §8.2).
    2. Build `Graph` topology (create `Node` instances via `NodeRegistry`,
       `add_node`, `add_edge` — edges are plain topology).
    3. Run `TopologyValidator` on the spec.
    4. Call `graph.compile(max_iterations, scheduler, default_trigger)` →
       `CompiledGraph`.

    Does not create state; state belongs to the `GraphInstance` execution.

    Usage:

    ```python
    compiler = GraphSpecCompiler(node_registry, state_classes)
    compiled: CompiledGraph[Any] = compiler.compile(spec)
    ```
    """

    def __init__(
        self,
        node_registry: NodeRegistry,
        state_classes: Mapping[str, type[GraphState]],
        validator: TopologyValidator | None = None,
        state_schema_compiler: Callable[[dict[str, FieldSpec]], type[GraphState]]
        | None = None,
    ) -> None:
        """Initialize the compiler with the required registries.

        Args:
            node_registry: registry of `NodeFactory` by `node_type` string.
                Used to materialize `NodeSpec` → `Node` instances.
            state_classes: mapping from serialized registry names to concrete
                `GraphState` subclasses.
            validator: optional `TopologyValidator` override. If `None`,
                a shared default instance is used. Injecting a custom
                validator is primarily for tests.
            state_schema_compiler: optional callable that resolves a
                declarative `state_schema` dict into a dynamic `GraphState`
                subclass (SPEC §8.2). Required when compiling a `GraphSpec`
                that sets `state_schema`; if `None` and the spec uses
                `state_schema`, `compile()` raises `ValueError`. The actual
                compilation logic (resolving custom types from
                `DATA_NAMESPACE`) lives on the modex_agent side —
                modex_graph only calls the injected callable.
        """
        self._node_registry = node_registry
        self._state_classes = state_classes
        self._validator = validator
        self._state_schema_compiler = state_schema_compiler

    def validate(self, spec: GraphSpec) -> None:
        """Validate topology without materializing graph nodes or edges."""
        validator = self._validator if self._validator is not None else _default_validator()
        validator.validate(spec)

    def compile(self, spec: GraphSpec) -> CompiledGraph[Any]:
        """Compile a `GraphSpec` into a `CompiledGraph`.

        Raises:
            TopologyError: if topology validation fails.
            KeyError: if a `NodeSpec.node_type` is not registered in the
                `NodeRegistry`.
            ValueError: if `spec.state_class` is not in the state-class
                mapping, or if `spec.state_schema` is set but no
                `state_schema_compiler` was injected.
            pydantic.ValidationError: if a `NodeSpec.config` fails
                validation against the factory's `config_schema()`.
            RoutingError: if `Graph.compile()` finds a structural issue that
                `TopologyValidator` did not catch (should not happen in
                practice — the validator is stricter).
        """
        self.resolve_state(spec)

        # State type is selected at runtime, so the graph is typed Any.
        graph: Graph[Any] = Graph(name=spec.name)

        node_specs = {node_spec.name: node_spec for node_spec in spec.nodes}
        node_specs.setdefault(
            GraphNode.START,
            NodeSpec(name=GraphNode.START, node_type="start"),
        )
        node_specs.setdefault(
            GraphNode.END,
            NodeSpec(name=GraphNode.END, node_type="end"),
        )

        # 3. Create and register all nodes, including executable START/END.
        for node_spec in node_specs.values():
            node = self._node_registry.create(node_spec)
            graph.add_node(node_spec.name, node)

        # 4. Add edges. EdgeSpec is topology-only (deliver/submit model).
        for edge in spec.edges:
            graph.add_edge(edge.source, edge.target)

        # 5. Validate topology (runs BEFORE graph.compile() to fail fast
        # on topology issues before the Graph builder's own checks).
        self.validate(spec)

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

    def resolve_state(self, spec: GraphSpec) -> type[GraphState]:
        """Resolve the state class for a spec via either declared path.

        Dispatches on which of `state_schema` / `state_class` is set (the
        `GraphSpec` validator guarantees exactly one is). Returns the
        resolved `GraphState` subclass. `compile()` calls this to validate
        the spec's state declaration is resolvable; the orchestrator calls
        it at run time to construct fresh state instances (both
        `state_class` and `state_schema` specs).
        """
        if spec.state_schema is not None:
            if self._state_schema_compiler is None:
                raise ValueError(
                    "GraphSpec.state_schema is set but no state_schema_compiler "
                    "was injected into GraphSpecCompiler. modex_graph does not "
                    "contain schema-compilation logic (ADR-0033 D11 / SPEC §8.2); "
                    "the compiler must be injected from the outside (modex_agent "
                    "side)."
                )
            return self._state_schema_compiler(spec.state_schema)
        # `state_class` is set (mutual exclusivity guaranteed by GraphSpec).
        assert spec.state_class is not None  # noqa: S101
        return self._resolve_state_class(spec.state_class)

    def _resolve_state_class(self, name: str) -> type[GraphState]:
        """Resolve a state class name or raise the compiler's validation error."""
        state_class = self._state_classes.get(name)
        if state_class is None:
            raise ValueError(
                f"State class {name!r} is not registered. "
                f"Registered names: {sorted(self._state_classes)}."
            )
        return state_class


__all__ = ["GraphSpecCompiler"]
