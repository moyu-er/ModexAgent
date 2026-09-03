"""Tests for ``build_state_schema_compiler`` -- FW graph state_schema compiler.

Verifies that the compiler:
- Resolves built-in types (string/int/float/bool/list/dict) to Python types.
- Resolves custom types via ``ComponentRegistry.resolve_namespace_model``.
- Handles list ``item_type`` (built-in and custom).
- Raises ``ValueError`` for unknown types.
- Uses ``initial`` values as defaults; type-appropriate defaults otherwise.
- Produces a ``GraphState`` subclass that can be instantiated.
- Integrates with ``GraphSpecCompiler.compile``.
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from pydantic import BaseModel

from modex_agent.plugins.abc import ComponentSlot, SimpleFactory
from modex_agent.plugins.assembly.graph_schema import build_state_schema_compiler
from modex_agent.plugins.registry import ComponentRegistry
from modex_graph import (
    EdgeSpec,
    FieldSpec,
    GraphNode,
    GraphSpec,
    GraphSpecCompiler,
    GraphState,
    NodeRegistry,
    NodeSpec,
)


class _DummyConfig(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}


class _ResearchNote(BaseModel):
    model_config = {"frozen": True, "extra": "forbid"}
    content: str = ""
    priority: int = 0


def _make_namespace_factory(model_cls: type[BaseModel]) -> SimpleFactory:
    return SimpleFactory(instance=model_cls, config_model=_DummyConfig)


def _registry_with_custom(*names: str) -> ComponentRegistry:
    registry = ComponentRegistry()
    for name in names:
        registry.register(
            ComponentSlot.DATA_NAMESPACE,
            name,
            _make_namespace_factory(_ResearchNote),
            overwrite=True,
        )
    return registry


class TestBuildStateSchemaCompilerBuiltins:
    def test_returns_callable(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        assert callable(compiler)

    def test_empty_schema_produces_graph_state_subclass(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({})
        assert issubclass(state_cls, GraphState)
        instance = state_cls()
        assert isinstance(instance, GraphState)
        assert instance.resume_target is None
        assert instance.node_scratch == {}

    @pytest.mark.parametrize(
        ("type_name", "py_type", "default"),
        [
            ("string", str, ""),
            ("int", int, 0),
            ("float", float, 0.0),
            ("bool", bool, False),
        ],
    )
    def test_scalar_builtins(
        self, type_name: str, py_type: type, default: Any
    ) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"field": FieldSpec(type=type_name)})
        assert state_cls.model_fields["field"].annotation is py_type
        instance = state_cls()
        assert instance.field == default

    def test_list_no_item_type(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"items": FieldSpec(type="list")})
        assert state_cls.model_fields["items"].annotation is list
        instance = state_cls()
        assert instance.items == []

    def test_dict_type(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"meta": FieldSpec(type="dict")})
        assert state_cls.model_fields["meta"].annotation is dict
        instance = state_cls()
        assert instance.meta == {}

    def test_list_with_builtin_item_type(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"tags": FieldSpec(type="list", item_type="string")})
        assert state_cls.model_fields["tags"].annotation == list[str]
        instance = state_cls()
        assert instance.tags == []

    def test_list_with_int_item_type(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"counts": FieldSpec(type="list", item_type="int")})
        assert state_cls.model_fields["counts"].annotation == list[int]


class TestBuildStateSchemaCompilerCustomTypes:
    def test_custom_type_resolved_from_data_namespace(self) -> None:
        registry = _registry_with_custom("research_note")
        compiler = build_state_schema_compiler(registry)
        state_cls = compiler({"note": FieldSpec(type="research_note")})
        annotation = state_cls.model_fields["note"].annotation
        assert _ResearchNote in get_args(annotation)
        assert type(None) in get_args(annotation)
        instance = state_cls()
        assert instance.note is None

    def test_custom_type_with_initial(self) -> None:
        registry = _registry_with_custom("research_note")
        compiler = build_state_schema_compiler(registry)
        initial = _ResearchNote(content="hello", priority=5)
        state_cls = compiler(
            {"note": FieldSpec(type="research_note", initial=initial)}
        )
        assert state_cls.model_fields["note"].annotation is _ResearchNote
        instance = state_cls()
        assert instance.note == initial

    def test_list_with_custom_item_type(self) -> None:
        registry = _registry_with_custom("research_note")
        compiler = build_state_schema_compiler(registry)
        state_cls = compiler(
            {"notes": FieldSpec(type="list", item_type="research_note")}
        )
        assert state_cls.model_fields["notes"].annotation == list[_ResearchNote]
        instance = state_cls()
        assert instance.notes == []


class TestBuildStateSchemaCompilerErrors:
    def test_unknown_type_raises_value_error(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        with pytest.raises(ValueError, match="Unknown state_schema type"):
            compiler({"bad": FieldSpec(type="bogus_type")})

    def test_unknown_item_type_raises_value_error(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        with pytest.raises(ValueError, match="Unknown state_schema type"):
            compiler({"bad": FieldSpec(type="list", item_type="bogus_type")})

    def test_error_message_includes_field_name(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        with pytest.raises(ValueError, match="my_field"):
            compiler({"my_field": FieldSpec(type="nonexistent")})

    def test_error_message_lists_builtin_types(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        with pytest.raises(ValueError, match="bool.*dict.*float.*int.*list.*string"):
            compiler({"bad": FieldSpec(type="nonexistent")})


class TestBuildStateSchemaCompilerInitialValues:
    def test_string_initial(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"title": FieldSpec(type="string", initial="hello")})
        instance = state_cls()
        assert instance.title == "hello"

    def test_int_initial(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"count": FieldSpec(type="int", initial=42)})
        instance = state_cls()
        assert instance.count == 42

    def test_list_initial(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler(
            {"items": FieldSpec(type="list", item_type="string", initial=["a", "b"])}
        )
        instance = state_cls()
        assert instance.items == ["a", "b"]


class TestBuildStateSchemaCompilerInstantiation:
    def test_compiled_state_accepts_field_values(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler(
            {
                "title": FieldSpec(type="string", initial=""),
                "count": FieldSpec(type="int", initial=0),
            }
        )
        instance = state_cls.model_validate({"title": "custom", "count": 10})
        assert instance.title == "custom"
        assert instance.count == 10

    def test_compiled_state_is_mutable(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"count": FieldSpec(type="int", initial=0)})
        instance = state_cls()
        instance.count = 99
        assert instance.count == 99

    def test_compiled_state_rejects_extra_fields(self) -> None:
        compiler = build_state_schema_compiler(ComponentRegistry())
        state_cls = compiler({"count": FieldSpec(type="int", initial=0)})
        with pytest.raises(Exception):  # ValidationError
            state_cls.model_validate({"unknown_field": "bad"})

    def test_multiple_fields_compiled(self) -> None:
        registry = _registry_with_custom("research_note")
        compiler = build_state_schema_compiler(registry)
        state_cls = compiler(
            {
                "title": FieldSpec(type="string", initial=""),
                "count": FieldSpec(type="int", initial=0),
                "active": FieldSpec(type="bool", initial=False),
                "tags": FieldSpec(type="list", item_type="string", initial=[]),
                "meta": FieldSpec(type="dict", initial={}),
                "note": FieldSpec(type="research_note"),
            }
        )
        instance = state_cls()
        assert instance.title == ""
        assert instance.count == 0
        assert instance.active is False
        assert instance.tags == []
        assert instance.meta == {}
        assert instance.note is None


class TestBuildStateSchemaCompilerIntegration:
    def test_compiler_injected_into_graph_spec_compiler(self) -> None:
        registry = _registry_with_custom("research_note")
        state_schema_compiler = build_state_schema_compiler(registry)

        nodes = NodeRegistry()
        compiler = GraphSpecCompiler(
            nodes,
            {},
            state_schema_compiler=state_schema_compiler,
        )
        spec = GraphSpec(
            name="test-graph",
            nodes=[NodeSpec(name="a", node_type="start")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
            state_schema={
                "title": FieldSpec(type="string", initial=""),
                "count": FieldSpec(type="int", initial=0),
                "note": FieldSpec(type="research_note"),
            },
        )

        compiled = compiler.compile(spec)
        assert compiled is not None

    def test_state_class_path_still_works_without_compiler(self) -> None:
        from modex_graph import DefaultGraphState

        nodes = NodeRegistry()
        compiler = GraphSpecCompiler(nodes, {"default": DefaultGraphState})
        spec = GraphSpec(
            name="test-graph",
            nodes=[NodeSpec(name="a", node_type="start")],
            edges=[
                EdgeSpec(source=GraphNode.START, target="a"),
                EdgeSpec(source="a", target=GraphNode.END),
            ],
            state_class="default",
        )
        compiled = compiler.compile(spec)
        assert compiled is not None
