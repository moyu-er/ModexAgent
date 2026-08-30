"""FW graph state_schema compiler -- resolves DATA_NAMESPACE types for graph state.

SPEC section 8.3 principle 4: the ``state_schema_compiler`` is the connection
between ``DATA_NAMESPACE`` (plugin-registered Pydantic models) and graph
``state_schema`` (declarative field shapes). The compiler is injected into
``GraphSpecCompiler`` (modex_graph) and called at compile time to turn a
``dict[str, FieldSpec]`` into a dynamic ``GraphState`` subclass.

Built-in types (string/int/float/bool/list/dict) map to Python types directly.
Custom types are resolved via ``ComponentRegistry.resolve_namespace_model(name)``
which returns the Pydantic model class registered in the ``DATA_NAMESPACE``
slot (``SimpleFactory`` wrapping the class itself, not an instance).

This module lives in ``modex_agent`` (not ``modex_graph``) because
``modex_graph`` is framework-agnostic and cannot import
``ComponentRegistry`` (ADR-0033 D11 / SPEC section 8.2). The compiler is
injected from the outside -- modex_graph only calls the injected callable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import Field, create_model

from modex_agent.plugins.registry import ComponentNotFoundError, ComponentRegistry
from modex_graph import FieldSpec, GraphState

# Built-in type name -> Python type. These are the primitive types that
# FieldSpec.type can carry without needing DATA_NAMESPACE resolution.
_BUILTIN_TYPES: dict[str, type] = {
    "string": str,
    "int": int,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
}

# Default zero values for scalar built-in types when FieldSpec.initial is
# None. Mutable types (list, dict) use Field(default_factory=...) to avoid
# shared mutable defaults across instances (Pydantic anti-pattern).
_ZERO_DEFAULTS: dict[str, Any] = {
    "string": "",
    "int": 0,
    "float": 0.0,
    "bool": False,
}


def _resolve_type_name(
    type_name: str,
    registry: ComponentRegistry,
    field_name: str,
) -> type:
    """Resolve a type name to a Python type.

    Built-in types map directly. Custom types resolve via
    ``registry.resolve_namespace_model`` (DATA_NAMESPACE slot). Unknown
    types raise ``ValueError`` with an actionable message.
    """
    if type_name in _BUILTIN_TYPES:
        return _BUILTIN_TYPES[type_name]
    try:
        return registry.resolve_namespace_model(type_name)
    except ComponentNotFoundError:
        raise ValueError(
            f"Unknown state_schema type {type_name!r} for field "
            f"{field_name!r}. Built-in types: {sorted(_BUILTIN_TYPES)}. "
            f"Custom types must be registered in the DATA_NAMESPACE slot."
        ) from None


def _resolve_field(
    name: str,
    spec: FieldSpec,
    registry: ComponentRegistry,
) -> tuple[type, Any]:
    """Resolve a FieldSpec to a ``(type, default)`` tuple for ``create_model``.

    For list types with ``item_type``, the element type is resolved and the
    field type becomes ``list[item_type]``.

    Defaults:
    - ``initial`` is not ``None`` -> use it directly.
    - ``list`` / ``dict`` with no initial -> ``Field(default_factory=...)``.
    - Scalar built-ins with no initial -> type-appropriate zero value.
    - Custom Pydantic model with no initial -> ``Model | None`` with
      ``None`` default (optional field).
    """
    type_name = spec.type

    # Resolve the Python type.
    if type_name == "list":
        if spec.item_type is None:
            py_type: type = list
        else:
            item_type = _resolve_type_name(spec.item_type, registry, name)
            py_type = list[item_type]  # type: ignore[valid-type]
    else:
        py_type = _resolve_type_name(type_name, registry, name)

    # Resolve the default.
    if spec.initial is not None:
        default: Any = spec.initial
    elif type_name == "list":
        default = Field(default_factory=list)
    elif type_name == "dict":
        default = Field(default_factory=dict)
    elif type_name in _ZERO_DEFAULTS:
        default = _ZERO_DEFAULTS[type_name]
    else:
        # Custom Pydantic model with no initial -- make optional.
        py_type = py_type | None  # type: ignore[assignment, valid-type]
        default = None

    return (py_type, default)


def _compile_schema(
    schema: dict[str, FieldSpec],
    registry: ComponentRegistry,
) -> type[GraphState]:
    """Build a dynamic ``GraphState`` subclass from a state_schema dict.

    Each field in *schema* is resolved to a ``(type, default)`` pair and
    passed to ``pydantic.create_model`` with ``__base__=GraphState``. The
    resulting class inherits ``GraphState``'s config (mutable,
    ``extra="forbid"``, ``arbitrary_types_allowed=True``) plus the
    ``resume_target`` / ``node_scratch`` framework fields.
    """
    fields: dict[str, tuple[type, Any]] = {}
    for name, spec in schema.items():
        fields[name] = _resolve_field(name, spec, registry)

    # create_model returns type[BaseModel]; with __base__=GraphState the
    # result is a GraphState subclass. The type checker conflates
    # **fields (tuple[type, Any] values) with the __config__/__doc__/etc.
    # keyword params, so suppress the overload/arg-type warnings -- at
    # runtime Pydantic routes **field_definitions correctly.
    model = create_model(  # type: ignore[call-overload]
        "DynamicGraphState",
        __base__=GraphState,
        **fields,  # type: ignore[arg-type]
    )
    return model  # type: ignore[return-value]


def build_state_schema_compiler(
    registry: ComponentRegistry,
) -> Callable[[dict[str, FieldSpec]], type[GraphState]]:
    """Create a state_schema compiler bound to *registry*.

    Returns a callable that accepts a ``dict[str, FieldSpec]`` (field name
    -> field shape) and returns a dynamic ``GraphState`` subclass with the
    resolved fields. The callable is intended for injection into
    ``GraphSpecCompiler(state_schema_compiler=...)``.

    The compiler resolves:
    - Built-in types (string/int/float/bool/list/dict) -> Python types.
    - Custom types -> ``registry.resolve_namespace_model(name)`` (Pydantic
      model class from the ``DATA_NAMESPACE`` slot).
    - List ``item_type`` -> resolved element type (built-in or custom).

    Unknown type names raise ``ValueError`` with the field name and valid
    built-in type list.
    """

    def _compiler(schema: dict[str, FieldSpec]) -> type[GraphState]:
        return _compile_schema(schema, registry)

    return _compiler


__all__ = ["build_state_schema_compiler"]
