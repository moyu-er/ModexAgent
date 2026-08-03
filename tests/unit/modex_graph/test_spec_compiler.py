# ruff: noqa: ANN401
"""Tests for `GraphSpecCompiler` (ticket 08)."""

from __future__ import annotations

from typing import Any, cast

import pytest

# Import test state from helpers to avoid duplicating fixtures.
from helpers import CounterState  # type: ignore[import-not-found]
from pydantic import BaseModel, ValidationError

from modex_graph import (
    CompiledGraph,
    EdgeSpec,
    GraphNode,
    GraphSpec,
    GraphSpecCompiler,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeResult,
    NodeSpec,
    SimpleStateFactory,
    StateFactory,
    StateFieldSpec,
    StateRegistry,
    StateSchema,
    TopologyError,
    TopologyValidator,
)

# ── Test fixtures: NodeFactory + config schema ─────────────────────────


class _NoOpNode(Node[CounterState]):
    """Minimal Node that records its `message` config for assertions."""

    message: str = "default"

    def __init__(self, message: str = "default") -> None:
        self.message = message

    def execute(self, ctx: Any, integrated_input: IntegratedInput) -> NodeResult:  # type: ignore[override]
        return NodeResult()


class _NoOpConfig(BaseModel):
    message: str = "default"


class _NoOpFactory(NodeFactory):
    """Factory that creates `_NoOpNode` from a config with `message`."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        message = spec.config.get("message", "default")
        return _NoOpNode(message=str(message))

    def config_schema(self) -> type[BaseModel] | None:
        return _NoOpConfig


class _RecordingValidator(TopologyValidator):
    """Validator that records validate() calls + can force a failure."""

    def __init__(self, *, fail_with: TopologyError | None = None) -> None:
        super().__init__()
        self.calls: list[GraphSpec] = []
        self._fail_with = fail_with

    def validate(  # type: ignore[override]
        self,
        spec: GraphSpec,
        *,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> None:
        self.calls.append(spec)
        if self._fail_with is not None:
            raise self._fail_with
        super().validate(spec, max_depth=max_depth, max_nodes=max_nodes)


def _schema() -> StateSchema:
    return StateSchema(
        name="test_state",
        fields=[StateFieldSpec(name="count", field_type="int", default=0)],
    )


def _node(name: str, **config: Any) -> NodeSpec:
    return NodeSpec(name=name, node_type="noop", config=config)


def _spec(
    nodes: list[NodeSpec],
    edges: list[EdgeSpec],
    *,
    state_schema: StateSchema | str | None = None,
    name: str = "test_graph",
) -> GraphSpec:
    return GraphSpec(
        name=name,
        nodes=nodes,
        edges=edges,
        state_schema=state_schema if state_schema is not None else _schema(),
    )


def _registries() -> tuple[NodeRegistry, StateRegistry]:
    nodes = NodeRegistry()
    nodes.register("noop", _NoOpFactory())
    states = StateRegistry()
    return nodes, states


# ── Tests ──────────────────────────────────────────────────────────────


class TestCompileValidSpec:
    def test_returns_compiled_graph(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("entry")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
        )
        compiled = compiler.compile(spec)
        assert isinstance(compiled, CompiledGraph)

    def test_compiled_graph_has_correct_nodes(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a"), _node("b"), _node("c")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target="c"),
                EdgeSpec(source="c", target=GraphNode.END),
            ],
        )
        compiled = compiler.compile(spec)
        assert set(compiled.nodes.keys()) == {"a", "b", "c"}
        for node in compiled.nodes.values():
            assert isinstance(node, _NoOpNode)

    def test_compiled_graph_has_correct_edges(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a"), _node("b")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target="b"),
                EdgeSpec(source="b", target=GraphNode.END),
            ],
        )
        compiled = compiler.compile(spec)
        edge_pairs = [(e.source, e.target) for e in compiled.edges]
        assert edge_pairs == [
            (GraphNode.START, "a"),
            ("a", "b"),
            ("b", GraphNode.END),
        ]

    def test_compiled_graph_edges_are_plain_topology(self) -> None:
        """All edges from EdgeSpec are plain topology (no reason field)."""
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
        )
        compiled = compiler.compile(spec)
        for edge in compiled.edges:
            assert edge.source in (GraphNode.START, "a")
            assert edge.target in ("a", GraphNode.END)

    def test_compiled_graph_name_from_spec(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
            name="my_pipeline",
        )
        compiled = compiler.compile(spec)
        assert compiled.name == "my_pipeline"

    def test_compiled_graph_max_iterations_from_spec(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
        )
        # Override max_iterations via the spec.
        spec = spec.model_copy(update={"max_iterations": 50})
        compiled = compiler.compile(spec)
        assert compiled.max_iterations == 50

    def test_node_config_passed_through(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("entry", message="hello")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="entry"),
                EdgeSpec(source="entry", target=GraphNode.END),
            ],
        )
        compiled = compiler.compile(spec)
        entry_node = cast(_NoOpNode, compiled.nodes["entry"])
        assert entry_node.message == "hello"

    def test_react_cycle_compiles(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("llm"), _node("tool")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="llm"),
                EdgeSpec(source="llm", target="tool"),
                EdgeSpec(source="tool", target="llm"),
                EdgeSpec(source="llm", target=GraphNode.END),
            ],
        )
        compiled = compiler.compile(spec)
        assert compiled.entry_node == "llm"


class TestUnregisteredNodeType:
    def test_unregistered_node_type_raises_keyerror(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[NodeSpec(name="n1", node_type="nonexistent")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="n1"),
                EdgeSpec(source="n1", target=GraphNode.END),
            ],
        )
        with pytest.raises(KeyError, match="not registered"):
            compiler.compile(spec)


class TestInvalidNodeConfig:
    def test_invalid_node_config_raises_validation_error(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("n1", message=["not", "a", "string"])],
            edges=[
                EdgeSpec(source=GraphNode.START, target="n1"),
                EdgeSpec(source="n1", target=GraphNode.END),
            ],
        )
        with pytest.raises(ValidationError):
            compiler.compile(spec)


class TestStateSchemaResolution:
    def test_inline_state_schema_resolves(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
            state_schema=_schema(),
        )
        compiled = compiler.compile(spec)
        assert isinstance(compiled, CompiledGraph)

    def test_registered_state_schema_name_resolves(self) -> None:
        nodes, states = _registries()
        states.register("my_state", SimpleStateFactory(CounterState))
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
            state_schema="my_state",
        )
        compiled = compiler.compile(spec)
        assert isinstance(compiled, CompiledGraph)

    def test_unregistered_state_schema_name_raises_valueerror(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
            state_schema="nonexistent_schema",
        )
        with pytest.raises(ValueError, match="not registered"):
            compiler.compile(spec)

    def test_bad_inline_state_schema_raises(self) -> None:
        """DynamicStateFactory construction fails on unresolvable types."""
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)

        bad_schema = StateSchema(
            name="bad_state",
            fields=[
                StateFieldSpec(name="x", field_type="NonExistentType"),
            ],
        )
        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
            state_schema=bad_schema,
        )
        with pytest.raises(ValueError, match="Cannot resolve"):
            compiler.compile(spec)


class TestTopologyErrorPropagation:
    def test_topology_error_propagates_from_validator(self) -> None:
        nodes, states = _registries()
        forced = TopologyError("forced topology failure")
        compiler = GraphSpecCompiler(nodes, states, validator=_RecordingValidator(fail_with=forced))

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="forced topology failure"):
            compiler.compile(spec)

    def test_default_validator_used_when_none_injected(self) -> None:
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)
        # No validator injected — uses the shared default TopologyValidator.
        # A valid spec compiles without error.
        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
        )
        compiled = compiler.compile(spec)
        assert isinstance(compiled, CompiledGraph)

    def test_validator_receives_spec(self) -> None:
        nodes, states = _registries()
        recorder = _RecordingValidator()
        compiler = GraphSpecCompiler(nodes, states, validator=recorder)

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
        )
        compiler.compile(spec)
        assert len(recorder.calls) == 1
        assert recorder.calls[0] is spec

    def test_topology_error_runs_before_graph_compile(self) -> None:
        """TopologyValidator runs before graph.compile() — fails fast."""
        nodes, states = _registries()
        forced = TopologyError("fail before compile")
        compiler = GraphSpecCompiler(nodes, states, validator=_RecordingValidator(fail_with=forced))

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
        )
        with pytest.raises(TopologyError, match="fail before compile"):
            compiler.compile(spec)


class TestStateNotCreated:
    def test_compile_does_not_create_state(self) -> None:
        """Compiler must not call StateFactory.create_state (state is at GraphInstance)."""
        nodes, states = _registries()

        class _TrackingFactory(StateFactory):
            """Factory that records create_state calls."""

            def __init__(self) -> None:
                self.create_calls = 0

            def create_state(self) -> Any:
                self.create_calls += 1
                return CounterState()

            def state_schema(self) -> StateSchema:
                return _schema()

            def restore_state(self, data: dict[str, Any]) -> Any:
                return CounterState()

        tracking = _TrackingFactory()
        states.register("tracking", tracking)
        compiler = GraphSpecCompiler(nodes, states)

        spec = _spec(
            nodes=[_node("a")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
            state_schema="tracking",
        )
        compiler.compile(spec)
        assert tracking.create_calls == 0
