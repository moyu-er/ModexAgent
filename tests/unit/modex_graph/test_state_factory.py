# ruff: noqa: ANN401
"""Tests for `StateFactory` ABC + `StateRegistry` + `SimpleStateFactory` + `DynamicStateFactory`."""

from __future__ import annotations

from typing import Annotated, Any, cast

import pytest

from modex_graph import (
    DynamicStateFactory,
    GraphState,
    LastValue,
    ReducerChannel,
    SimpleStateFactory,
    StateFactory,
    StateFieldSpec,
    StateRegistry,
    StateSchema,
)

# ── Test fixtures: pre-defined GraphState subclasses ───────────────────


class _SimpleState(GraphState):
    """State with LastValue fields only."""

    count: Annotated[int, LastValue] = 0
    name: Annotated[str, LastValue] = "default"


class _ReducerState(GraphState):
    """State with a ReducerChannel field (list accumulation)."""

    items: Annotated[list[str], ReducerChannel(reducer=lambda a, b: a + b)] = []
    total: Annotated[int, LastValue] = 0


def _simple(factory: SimpleStateFactory | StateRegistry, *args: str) -> _SimpleState:
    """Cast helper: create via factory/registry and narrow to `_SimpleState`."""
    if isinstance(factory, StateRegistry):
        return cast(_SimpleState, factory.create_state(args[0]))
    return cast(_SimpleState, factory.create_state())


# ── StateFactory ABC ──────────────────────────────────────────────────


class TestStateFactoryABC:
    """`StateFactory` ABC — cannot be instantiated directly."""

    def test_abc_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            StateFactory()  # type: ignore[abstract]

    def test_subclass_must_implement_all_three_methods(self) -> None:
        class _MissingOne(StateFactory):
            def create_state(self) -> GraphState:
                return _SimpleState()

            def state_schema(self) -> StateSchema:
                return StateSchema(name="x", fields=[])

        with pytest.raises(TypeError):
            _MissingOne()  # type: ignore[abstract]


# ── SimpleStateFactory ────────────────────────────────────────────────


class TestSimpleStateFactory:
    """`SimpleStateFactory` — wraps a pre-defined GraphState subclass."""

    def test_create_state_returns_fresh_instance(self) -> None:
        factory = SimpleStateFactory(_SimpleState)
        state = _simple(factory)
        assert isinstance(state, _SimpleState)
        assert state.count == 0
        assert state.name == "default"

    def test_create_state_each_call_is_new_instance(self) -> None:
        factory = SimpleStateFactory(_SimpleState)
        s1 = _simple(factory)
        s2 = _simple(factory)
        assert s1 is not s2

    def test_restore_state_round_trip(self) -> None:
        factory = SimpleStateFactory(_SimpleState)
        state = _simple(factory)
        state.count = 42
        state.name = "modified"

        checkpoint = state.checkpoint()
        restored = cast(_SimpleState, factory.restore_state(checkpoint))
        assert restored.count == 42
        assert restored.name == "modified"

    def test_state_schema_introspects_fields(self) -> None:
        factory = SimpleStateFactory(_SimpleState)
        schema = factory.state_schema()
        assert schema.name == "_SimpleState"
        field_names = [f.name for f in schema.fields]
        assert "count" in field_names
        assert "name" in field_names
        assert "resume_target" not in field_names

    def test_state_schema_field_types(self) -> None:
        factory = SimpleStateFactory(_SimpleState)
        schema = factory.state_schema()
        count_field = next(f for f in schema.fields if f.name == "count")
        assert count_field.field_type == "int"
        assert count_field.channel == "last_value"
        assert count_field.default == 0

    def test_state_schema_reducer_channel(self) -> None:
        factory = SimpleStateFactory(_ReducerState)
        schema = factory.state_schema()
        items_field = next(f for f in schema.fields if f.name == "items")
        assert items_field.channel == "reducer"

    def test_restore_preserves_reducer_state(self) -> None:
        factory = SimpleStateFactory(_ReducerState)
        state = cast(_ReducerState, factory.create_state())
        state.apply_state_update({"items": ["a"]})
        state.apply_state_update({"items": ["b"]})

        checkpoint = state.checkpoint()
        restored = cast(_ReducerState, factory.restore_state(checkpoint))
        assert restored.items == ["a", "b"]


# ── DynamicStateFactory ───────────────────────────────────────────────


class TestDynamicStateFactory:
    """`DynamicStateFactory` — builds GraphState subclass from StateSchema."""

    def test_create_state_with_last_value_fields(self) -> None:
        schema = StateSchema(
            name="DynamicState",
            fields=[
                StateFieldSpec(name="count", field_type="int", default=0),
                StateFieldSpec(name="name", field_type="str", default=""),
            ],
        )
        factory = DynamicStateFactory(schema)

        state = factory.create_state()
        assert isinstance(state, GraphState)
        assert _attr(state, "count") == 0
        assert _attr(state, "name") == ""

    def test_create_state_with_defaults(self) -> None:
        schema = StateSchema(
            name="WithDefaults",
            fields=[
                StateFieldSpec(name="x", field_type="int", default=10),
                StateFieldSpec(name="y", field_type="str", default="hello"),
            ],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        assert _attr(state, "x") == 10
        assert _attr(state, "y") == "hello"

    def test_create_state_each_call_is_new_instance(self) -> None:
        schema = StateSchema(
            name="Fresh",
            fields=[StateFieldSpec(name="x", field_type="int", default=0)],
        )
        factory = DynamicStateFactory(schema)
        s1 = factory.create_state()
        s2 = factory.create_state()
        assert s1 is not s2

    def test_class_is_cached(self) -> None:
        schema = StateSchema(
            name="Cached",
            fields=[StateFieldSpec(name="x", field_type="int", default=0)],
        )
        factory = DynamicStateFactory(schema)
        s1 = factory.create_state()
        s2 = factory.create_state()
        assert type(s1) is type(s2)

    def test_restore_state_round_trip(self) -> None:
        schema = StateSchema(
            name="Restorable",
            fields=[
                StateFieldSpec(name="count", field_type="int", default=0),
                StateFieldSpec(name="name", field_type="str", default=""),
            ],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        _set(state, "count", 99)
        _set(state, "name", "restored")

        checkpoint = state.checkpoint()
        restored = factory.restore_state(checkpoint)
        assert _attr(restored, "count") == 99
        assert _attr(restored, "name") == "restored"

    def test_state_schema_returns_input_schema(self) -> None:
        schema = StateSchema(
            name="SchemaReturn",
            fields=[StateFieldSpec(name="x", field_type="int", default=0)],
        )
        factory = DynamicStateFactory(schema)
        assert factory.state_schema() is schema

    def test_generic_types_resolved(self) -> None:
        schema = StateSchema(
            name="GenericState",
            fields=[
                StateFieldSpec(name="items", field_type="list[str]", channel="reducer", default=[]),
                StateFieldSpec(name="meta", field_type="dict[str, Any]", default={}),
            ],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        assert _attr(state, "items") == []
        assert _attr(state, "meta") == {}

    def test_reducer_channel_accumulates(self) -> None:
        schema = StateSchema(
            name="ReducerDyn",
            fields=[
                StateFieldSpec(name="items", field_type="list[str]", channel="reducer", default=[]),
            ],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        state.apply_state_update({"items": ["a"]})
        state.apply_state_update({"items": ["b", "c"]})
        assert _attr(state, "items") == ["a", "b", "c"]

    def test_reducer_int_addition(self) -> None:
        schema = StateSchema(
            name="IntReducer",
            fields=[
                StateFieldSpec(name="total", field_type="int", channel="reducer", default=0),
            ],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        state.apply_state_update({"total": 5})
        state.apply_state_update({"total": 10})
        assert _attr(state, "total") == 15

    def test_union_type_resolved(self) -> None:
        schema = StateSchema(
            name="UnionState",
            fields=[
                StateFieldSpec(name="optional_name", field_type="str | None", default=None),
            ],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        assert _attr(state, "optional_name") is None

    def test_unresolvable_type_raises(self) -> None:
        schema = StateSchema(
            name="BadType",
            fields=[StateFieldSpec(name="x", field_type="NonexistentType", default=None)],
        )
        with pytest.raises(ValueError, match="Cannot resolve field_type"):
            DynamicStateFactory(schema)

    def test_unresolvable_channel_raises(self) -> None:
        schema = StateSchema(
            name="BadChannel",
            fields=[StateFieldSpec(name="x", field_type="int", channel="unknown_channel")],
        )
        with pytest.raises(ValueError, match="Cannot resolve channel"):
            DynamicStateFactory(schema)

    def test_dynamic_state_is_graph_state_subclass(self) -> None:
        schema = StateSchema(
            name="IsGraphState",
            fields=[StateFieldSpec(name="x", field_type="int", default=0)],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        assert isinstance(state, GraphState)

    def test_dynamic_state_inherits_resume_target(self) -> None:
        schema = StateSchema(
            name="InheritsBase",
            fields=[StateFieldSpec(name="x", field_type="int", default=0)],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        assert state.resume_target is None

    def test_checkpoint_json_serializable(self) -> None:
        import json

        schema = StateSchema(
            name="JsonCheck",
            fields=[
                StateFieldSpec(name="count", field_type="int", default=0),
                StateFieldSpec(name="name", field_type="str", default=""),
            ],
        )
        factory = DynamicStateFactory(schema)
        state = factory.create_state()
        _set(state, "count", 42)
        _set(state, "name", "test")
        checkpoint = state.checkpoint()
        json_str = json.dumps(checkpoint)
        restored_data = json.loads(json_str)
        restored = factory.restore_state(restored_data)
        assert _attr(restored, "count") == 42
        assert _attr(restored, "name") == "test"


# ── Round-trip: SimpleStateFactory → StateSchema → DynamicStateFactory ──


class TestSchemaRoundTrip:
    """SimpleStateFactory.state_schema() → DynamicStateFactory should round-trip."""

    def test_simple_state_round_trip(self) -> None:
        simple_factory = SimpleStateFactory(_SimpleState)
        schema = simple_factory.state_schema()

        dynamic_factory = DynamicStateFactory(schema)
        dyn_state = dynamic_factory.create_state()

        assert _attr(dyn_state, "count") == 0
        assert _attr(dyn_state, "name") == "default"

        _set(dyn_state, "count", 100)
        _set(dyn_state, "name", "dynamic")
        checkpoint = dyn_state.checkpoint()

        restored_simple = cast(_SimpleState, simple_factory.restore_state(checkpoint))
        assert restored_simple.count == 100
        assert restored_simple.name == "dynamic"


# ── StateRegistry ─────────────────────────────────────────────────────


class TestStateRegistry:
    """`StateRegistry` — register factories by name."""

    def test_register_and_create(self) -> None:
        registry = StateRegistry()
        registry.register("simple", SimpleStateFactory(_SimpleState))
        assert registry.is_registered("simple")

        state = _simple(registry, "simple")
        assert isinstance(state, _SimpleState)

    def test_create_unknown_raises_keyerror(self) -> None:
        registry = StateRegistry()
        with pytest.raises(KeyError, match="not registered"):
            registry.create_state("nonexistent")

    def test_register_duplicate_raises_valueerror(self) -> None:
        registry = StateRegistry()
        registry.register("simple", SimpleStateFactory(_SimpleState))
        with pytest.raises(ValueError, match="already registered"):
            registry.register("simple", SimpleStateFactory(_SimpleState))

    def test_unregister(self) -> None:
        registry = StateRegistry()
        registry.register("simple", SimpleStateFactory(_SimpleState))
        assert registry.is_registered("simple")
        registry.unregister("simple")
        assert not registry.is_registered("simple")
        registry.unregister("simple")

    def test_registered_names_sorted(self) -> None:
        registry = StateRegistry()
        schema = StateSchema(
            name="dyn", fields=[StateFieldSpec(name="x", field_type="int", default=0)]
        )
        registry.register("zeta", SimpleStateFactory(_SimpleState))
        registry.register("alpha", DynamicStateFactory(schema))
        registry.register("mid", SimpleStateFactory(_ReducerState))
        assert registry.registered_names() == ["alpha", "mid", "zeta"]

    def test_restore_state(self) -> None:
        registry = StateRegistry()
        registry.register("simple", SimpleStateFactory(_SimpleState))

        state = _simple(registry, "simple")
        state.count = 55
        checkpoint = state.checkpoint()

        restored = cast(_SimpleState, registry.restore_state("simple", checkpoint))
        assert restored.count == 55

    def test_restore_unknown_raises_keyerror(self) -> None:
        registry = StateRegistry()
        with pytest.raises(KeyError, match="not registered"):
            registry.restore_state("nonexistent", {})

    def test_get_schema(self) -> None:
        registry = StateRegistry()
        registry.register("simple", SimpleStateFactory(_SimpleState))

        schema = registry.get_schema("simple")
        assert schema.name == "_SimpleState"
        assert len(schema.fields) == 2

    def test_get_schema_unknown_raises(self) -> None:
        registry = StateRegistry()
        with pytest.raises(KeyError, match="not registered"):
            registry.get_schema("nonexistent")

    def test_dynamic_factory_registered(self) -> None:
        registry = StateRegistry()
        schema = StateSchema(
            name="registered_dyn",
            fields=[StateFieldSpec(name="count", field_type="int", default=0)],
        )
        registry.register("dyn", DynamicStateFactory(schema))

        state = registry.create_state("dyn")
        assert _attr(state, "count") == 0

        _set(state, "count", 77)
        checkpoint = state.checkpoint()
        restored = registry.restore_state("dyn", checkpoint)
        assert _attr(restored, "count") == 77


# ── Helpers for dynamic state attribute access ────────────────────────
# DynamicStateFactory creates classes at runtime via pydantic.create_model.
# The type checker cannot see the dynamically-created fields, so we use
# `object.__getattribute__` / `object.__setattr__` to access them. This is
# a real extension boundary (runtime-built classes), not a violation of
# the no-getattr/hasattr rule (rule 6).


def _attr(state: GraphState, name: str) -> Any:
    """Read a field from a dynamically-built GraphState instance."""
    return object.__getattribute__(state, name)


def _set(state: GraphState, name: str, value: Any) -> None:
    """Write a field on a dynamically-built GraphState instance."""
    object.__setattr__(state, name, value)
