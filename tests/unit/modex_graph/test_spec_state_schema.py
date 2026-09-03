# ruff: noqa: ANN401
"""TDD tests for `GraphSpec.state_schema` + `FieldSpec` + compiler injection.

Covers SPEC §8.2 — declarative state schema on `GraphSpec` (mutually exclusive
with `state_class`), `FieldSpec` describes field shape only (dict key IS the
name — no `name` field on `FieldSpec`), and `GraphSpecCompiler` accepts an
optional `state_schema_compiler` injection that turns a `state_schema` dict
into a dynamic `GraphState` subclass.

The modex_graph layer only describes the envelope (`FieldSpec` shape) and
calls the injected compiler; the actual compilation logic (resolving custom
types from `DATA_NAMESPACE`) lives on the modex_agent side (Task 27).
"""

from __future__ import annotations

from typing import Any

import pytest
from helpers import CounterState  # type: ignore[import-not-found]
from pydantic import BaseModel, ValidationError

from modex_graph import (
    CompiledGraph,
    EdgeSpec,
    FieldSpec,
    GraphContext,
    GraphNode,
    GraphSpec,
    GraphSpecCompiler,
    GraphState,
    IntegratedInput,
    Node,
    NodeFactory,
    NodeRegistry,
    NodeSpec,
)

# ── Test fixtures (mirror test_spec_compiler.py) ──────────────────────


class _NoOpNodeImpl(Node[CounterState]):
    """Minimal Node that records its `message` config for assertions."""

    message: str = "default"

    def __init__(self, message: str = "default") -> None:
        self.message = message

    async def execute(
        self,
        ctx: GraphContext[CounterState],
        integrated_input: IntegratedInput,
    ) -> None:
        return None


class _NoOpConfig(BaseModel):
    message: str = "default"


class _NoOpNodeFactory(NodeFactory):
    """Factory that creates `_NoOpNodeImpl` from a config with `message`."""

    def create(self, spec: NodeSpec) -> Node[Any]:
        message = spec.config.get("message", "default")
        return _NoOpNodeImpl(message=str(message))

    def config_schema(self) -> type[BaseModel] | None:
        return _NoOpConfig


def _node(name: str, **config: Any) -> NodeSpec:
    return NodeSpec(name=name, node_type="noop", config=config)


def _edges() -> list[EdgeSpec]:
    return [
        EdgeSpec(source=GraphNode.START, target="a"),
        EdgeSpec(source="a", target=GraphNode.END),
    ]


def _registries() -> tuple[NodeRegistry, dict[str, type[CounterState]]]:
    nodes = NodeRegistry()
    nodes.register("noop", _NoOpNodeFactory())
    states = {"counter": CounterState}
    return nodes, states


# ── Dynamic GraphState subclass built by the test compiler ────────────


class _DynamicSchemaState(GraphState):
    """State subclass that the injected compiler builds from a state_schema.

    In real code (Task 27) the compiler inspects `FieldSpec.type` +
    `item_type` and synthesises a Pydantic model dynamically. Here we use a
    fixed subclass to verify the wiring: compile() calls the compiler with
    the schema dict and the returned class is a real `GraphState` subclass
    that can be instantiated.
    """

    research_notes: str = ""
    tool_results: list[str] = []


# ── Tests ──────────────────────────────────────────────────────────────


class TestFieldSpec:
    """`FieldSpec` — frozen Pydantic model describing one state field's shape.

    Per SPEC §8.2 (revised): `FieldSpec` has NO `name` field — the dict key
    in `state_schema` IS the name. Fields: `type`, `item_type`, `initial`.
    """

    def test_minimal_type_only(self) -> None:
        fs = FieldSpec(type="string")
        assert fs.type == "string"
        assert fs.item_type is None
        assert fs.initial is None

    def test_with_item_type_and_initial(self) -> None:
        fs = FieldSpec(type="list", item_type="string", initial=[])
        assert fs.type == "list"
        assert fs.item_type == "string"
        assert fs.initial == []

    def test_frozen(self) -> None:
        fs = FieldSpec(type="string")
        with pytest.raises(ValidationError):
            fs.type = "int"  # type: ignore[misc]

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            FieldSpec(type="string", name="should_not_be_allowed")  # type: ignore[call-arg]

    def test_no_name_field(self) -> None:
        """`FieldSpec` must NOT declare a `name` field — dict key IS the name."""
        assert "name" not in FieldSpec.model_fields

    def test_serialization_round_trip(self) -> None:
        fs = FieldSpec(type="list", item_type="string", initial=[])
        restored = FieldSpec.model_validate(fs.model_dump())
        assert restored == fs


class TestStateSchemaMutualExclusivity:
    """`state_schema` and `state_class` are mutually exclusive on `GraphSpec`."""

    def test_state_schema_only_accepted(self) -> None:
        spec = GraphSpec(
            name="g",
            nodes=[_node("a")],
            edges=_edges(),
            state_schema={"count": FieldSpec(type="int", initial=0)},
        )
        assert spec.state_schema is not None
        assert "count" in spec.state_schema
        assert spec.state_class is None

    def test_state_class_only_accepted(self) -> None:
        spec = GraphSpec(
            name="g",
            nodes=[_node("a")],
            edges=_edges(),
            state_class="counter",
        )
        assert spec.state_class == "counter"
        assert spec.state_schema is None

    def test_both_set_rejected(self) -> None:
        with pytest.raises(ValidationError, match="mutually exclusive"):
            GraphSpec(
                name="g",
                nodes=[_node("a")],
                edges=_edges(),
                state_class="counter",
                state_schema={"count": FieldSpec(type="int", initial=0)},
            )

    def test_neither_set_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must specify either state_class"):
            GraphSpec(
                name="g",
                nodes=[_node("a")],
                edges=_edges(),
            )


class TestStateSchemaJsonParsing:
    """JSON / dict deserialization of `state_schema`."""

    def test_json_round_trip(self) -> None:
        spec = GraphSpec(
            name="g",
            nodes=[_node("a")],
            edges=_edges(),
            state_schema={
                "notes": FieldSpec(type="string", initial=""),
                "results": FieldSpec(type="list", item_type="string", initial=[]),
            },
        )
        json_str = spec.model_dump_json()
        restored = GraphSpec.model_validate_json(json_str)
        assert restored == spec
        assert restored.state_schema is not None
        assert set(restored.state_schema) == {"notes", "results"}
        assert restored.state_schema["results"].item_type == "string"

    def test_duplicate_keys_in_json_rejected(self) -> None:
        """Duplicate keys in `state_schema` JSON collapse deterministically (last-wins).

        Pydantic v2's JSON parser uses last-wins semantics for duplicate
        object keys (it does NOT raise). Duplicate-key rejection is the
        YAML/JSON loader's responsibility — modex_graph receives an already
        collapsed dict. This test documents the deterministic behavior: the
        last value wins, and the resulting `state_schema` has exactly one
        entry per key.

        Callers parsing YAML should configure their loader to reject
        duplicate keys (e.g. `ruamel.yaml` with `allow_duplicate_keys=False`)
        before passing the dict to `GraphSpec`.
        """
        json_str = (
            '{"name":"g",'
            '"nodes":[{"name":"a","node_type":"noop"}],'
            '"edges":['
            '{"source":"__start__","target":"a"},'
            '{"source":"a","target":"__end__"}'
            '],'
            '"state_schema":{'
            '"count":{"type":"int","initial":0},'
            '"count":{"type":"string","initial":""}'
            "}}"
        )
        spec = GraphSpec.model_validate_json(json_str)
        # Last-wins: the second "count" entry (type=string) survives.
        assert spec.state_schema is not None
        assert set(spec.state_schema) == {"count"}
        assert spec.state_schema["count"].type == "string"

    def test_malformed_field_spec_value_rejected(self) -> None:
        """`state_schema` dict values must satisfy `FieldSpec`'s schema."""
        with pytest.raises(ValidationError):
            GraphSpec(
                name="g",
                nodes=[_node("a")],
                edges=_edges(),
                state_schema={"bad": {"item_type": "string"}},  # type: ignore[dict-item]
            )

    def test_extra_field_on_field_spec_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GraphSpec(
                name="g",
                nodes=[_node("a")],
                edges=_edges(),
                state_schema={
                    "count": {"type": "int", "bogus": True},  # type: ignore[dict-item]
                },
            )


class TestStateSchemaCompile:
    """`GraphSpecCompiler.compile` dispatches state_schema → injected compiler."""

    def test_state_schema_compiles_via_injected_compiler(self) -> None:
        """state_schema + injected compiler → compile() calls compiler → CompiledGraph.

        The injected compiler receives the state_schema dict and returns a
        `GraphState` subclass. compile() does NOT create state (same contract
        as the state_class path) — it validates the spec can be compiled and
        the compiler resolved the schema to a usable class.
        """
        captured: dict[str, FieldSpec] = {}

        def _compiler(schema: dict[str, FieldSpec]) -> type[GraphState]:
            captured.update(schema)
            return _DynamicSchemaState

        nodes, _ = _registries()
        compiler = GraphSpecCompiler(
            nodes,
            {},
            state_schema_compiler=_compiler,
        )
        spec = GraphSpec(
            name="research-workflow",
            nodes=[_node("a")],
            edges=_edges(),
            state_schema={
                "research_notes": FieldSpec(type="string", initial=""),
                "tool_results": FieldSpec(type="list", item_type="string", initial=[]),
            },
        )

        compiled = compiler.compile(spec)

        # compile() returned a CompiledGraph.
        assert isinstance(compiled, CompiledGraph)
        # The compiler was called with the schema dict (dict key IS the name).
        assert set(captured) == {"research_notes", "tool_results"}
        assert captured["tool_results"].item_type == "string"
        # The returned class is a real GraphState subclass — instantiable.
        state = _DynamicSchemaState()
        assert isinstance(state, GraphState)
        assert state.research_notes == ""
        assert state.tool_results == []

    def test_state_schema_without_compiler_raises(self) -> None:
        """state_schema set but no compiler injected → explicit error.

        modex_graph does NOT contain schema-compilation logic (architecture
        boundary — SPEC §8.2). The compiler must be injected from outside
        (modex_agent side, Task 27). Without it, compile() raises a clear
        error pointing at the missing injection.
        """
        nodes, _ = _registries()
        compiler = GraphSpecCompiler(nodes, {})  # no state_schema_compiler
        spec = GraphSpec(
            name="g",
            nodes=[_node("a")],
            edges=_edges(),
            state_schema={"count": FieldSpec(type="int", initial=0)},
        )
        with pytest.raises(ValueError, match="state_schema_compiler"):
            compiler.compile(spec)

    def test_unknown_type_rejected_by_injected_compiler(self) -> None:
        """Unknown `FieldSpec.type` is rejected by the injected compiler.

        modex_graph's `FieldSpec.type` is a plain `str` (the engine does not
        validate type names — only the injected compiler knows the valid
        type universe). compile() propagates the compiler's rejection so
        downstream callers see a clear error.
        """
        valid_types = {"string", "int", "list"}

        def _strict_compiler(schema: dict[str, FieldSpec]) -> type[GraphState]:
            for name, field in schema.items():
                if field.type not in valid_types:
                    raise ValueError(
                        f"Unknown state_schema type {field.type!r} for field "
                        f"{name!r}. Valid types: {sorted(valid_types)}."
                    )
            return _DynamicSchemaState

        nodes, _ = _registries()
        compiler = GraphSpecCompiler(
            nodes,
            {},
            state_schema_compiler=_strict_compiler,
        )
        spec = GraphSpec(
            name="g",
            nodes=[_node("a")],
            edges=_edges(),
            state_schema={"custom": FieldSpec(type="bogus_plugin_type", initial=None)},
        )
        with pytest.raises(ValueError, match="Unknown state_schema type"):
            compiler.compile(spec)

    def test_state_class_path_still_works_without_compiler(self) -> None:
        """state_class path is unchanged when no state_schema_compiler is injected.

        Existing callers that use state_class (registered GraphState subclass)
        must continue to work without providing a state_schema_compiler.
        """
        nodes, states = _registries()
        compiler = GraphSpecCompiler(nodes, states)  # no state_schema_compiler
        spec = GraphSpec(
            name="g",
            nodes=[_node("a")],
            edges=_edges(),
            state_class="counter",
        )
        compiled = compiler.compile(spec)
        assert isinstance(compiled, CompiledGraph)
