# ruff: noqa: ANN401
"""`StateFactory` ABC + `StateRegistry` + `SimpleStateFactory` + `DynamicStateFactory`.

Per ticket 08: a `StateFactory` creates and restores `GraphState` instances.
Factories are registered by name in a `StateRegistry`; a `GraphSpec` with
`state_schema = "my_schema"` references a registered factory by name.

Two generic implementations live here (in `modex_graph`):

- `SimpleStateFactory` — wraps a pre-defined `GraphState` subclass. The
  simplest path: business code declares `class MyState(GraphState): ...`
  and registers `SimpleStateFactory(MyState)` under a name. `create_state`
  returns `MyState()`; `restore_state` calls `MyState.from_checkpoint(data)`.

- `DynamicStateFactory` — builds a `GraphState` subclass at runtime from a
  `StateSchema`. Uses `pydantic.create_model` with `Annotated[T, channel]`
  fields matching the `StateFieldSpec` entries. The built class is cached
  so repeated `create_state()` calls are cheap.

Business-specific factories (e.g. `ReactStateFactory`) live in
`modex_agent` and register themselves with the `StateRegistry` at startup.
"""

from __future__ import annotations

import operator
from abc import ABC, abstractmethod
from typing import Annotated, Any, cast

from pydantic import create_model
from pydantic_core import PydanticUndefined

from .channel import BaseChannel, LastValue, ReducerChannel
from .state import GraphState
from .state_schema import StateFieldSpec, StateSchema

# ── Type resolution: string → Python type ──────────────────────────────
# Safe namespace for resolving `StateFieldSpec.field_type` strings.
# NEVER use bare `eval` on untrusted input — this namespace exposes only
# primitive types and `Any`. Generic forms like `list[str]`, `dict[str, Any]`,
# `int | None` are resolved via `eval` with this restricted namespace and
# no `__builtins__`.
_SAFE_TYPE_NAMESPACE: dict[str, Any] = {
    "int": int,
    "str": str,
    "float": float,
    "bool": bool,
    "list": list,
    "dict": dict,
    "set": set,
    "tuple": tuple,
    "bytes": bytes,
    "Any": Any,
    "None": type(None),
    "NoneType": type(None),
}


def _resolve_type(type_str: str) -> Any:
    """Resolve a `StateFieldSpec.field_type` string to a Python type.

    Direct lookup handles common primitives. Generic forms (`list[str]`,
    `dict[str, Any]`, `int | None`) are resolved via `eval` with a
    restricted namespace (no `__builtins__`, only primitive types + `Any`).

    Raises:
        ValueError: if `type_str` cannot be resolved.
    """
    # Fast path: direct primitive lookup.
    if type_str in _SAFE_TYPE_NAMESPACE:
        return _SAFE_TYPE_NAMESPACE[type_str]
    # Generic forms: eval with restricted namespace.
    try:
        return eval(  # noqa: S307 — restricted namespace, no __builtins__
            type_str,
            {"__builtins__": {}},
            dict(_SAFE_TYPE_NAMESPACE),
        )
    except Exception as exc:
        raise ValueError(
            f"Cannot resolve field_type {type_str!r}. Supported primitives: "
            f"{sorted(_SAFE_TYPE_NAMESPACE.keys())}, plus generic forms like "
            f"'list[str]', 'dict[str, Any]', 'int | None'."
        ) from exc


# ── Channel resolution: string → channel marker ───────────────────────
# Returns a channel marker usable as `Annotated[T, marker]` metadata.
# Class form (`LastValue`) is used where possible — `GraphState._setup_channels`
# instantiates it via `marker()._fresh(field_type)`. Instance form
# (`ReducerChannel(reducer=op.add)`) is used when the channel needs config.


def _resolve_channel(channel_str: str) -> BaseChannel[Any] | type[BaseChannel[Any]]:
    """Resolve a `StateFieldSpec.channel` string to a channel marker.

    Supported:
    - `"last_value"` → `LastValue` (class form).
    - `"reducer"` → `ReducerChannel(reducer=operator.add)` (instance form,
      default reducer is `operator.add` — list concat / int add).

    Future extension: a channel registry for custom channel names.

    Raises:
        ValueError: if `channel_str` is not a recognized channel kind.
    """
    if channel_str == "last_value":
        return LastValue
    if channel_str == "reducer":
        return ReducerChannel(reducer=operator.add)
    raise ValueError(
        f"Cannot resolve channel {channel_str!r}. Supported channels: "
        f"'last_value', 'reducer'. Custom channel registration is a future "
        f"extension."
    )


# ── Reverse: Python type → string (for SimpleStateFactory.state_schema) ──

_TYPE_NAME_BY_PYTHON: dict[Any, str] = {
    int: "int",
    str: "str",
    float: "float",
    bool: "bool",
    list: "list",
    dict: "dict",
    set: "set",
    tuple: "tuple",
    bytes: "bytes",
    type(None): "None",
    Any: "Any",
}


def _type_to_string(annotation: Any) -> str:
    """Convert a Python type annotation to a string for `StateFieldSpec.field_type`.

    Handles primitives (`int` → `"int"`), generic forms (`list[str]` →
    `"list[str]"`), union types (`int | None` → `"int | None"`), and `Any`
    (`Any` → `"Any"`). Falls back to `str(annotation)` for unrecognized types.
    """
    # Direct lookup for primitives + Any + NoneType.
    if annotation in _TYPE_NAME_BY_PYTHON:
        return _TYPE_NAME_BY_PYTHON[annotation]
    # Generic forms — str() gives clean output in Python 3.10+.
    # e.g. str(list[str]) -> "list[str]", str(dict[str, Any]) -> "dict[str, Any]",
    # str(int | None) -> "int | None".
    text = str(annotation)
    # Strip "typing." prefix for older typing forms (e.g. typing.List[str] → List[str]).
    # The forward direction is best-effort — DynamicStateFactory._resolve_type
    # handles both stripped and unstripped forms.
    return text.replace("typing.", "")


def _channel_marker_to_string(
    marker: BaseChannel[Any] | type[BaseChannel[Any]] | None,
) -> str:
    """Convert a channel marker to a string for `StateFieldSpec.channel`.

    - `LastValue` (class or instance) → `"last_value"`.
    - `ReducerChannel` (class or instance) → `"reducer"`.
    - `None` (no marker, defaults to LastValue) → `"last_value"`.
    - Unknown channel → lowercased class name (best-effort).
    """
    if marker is None:
        return "last_value"
    # Class form.
    if isinstance(marker, type):
        if issubclass(marker, LastValue):
            return "last_value"
        if issubclass(marker, ReducerChannel):
            return "reducer"
        return marker.__name__.lower()
    # Instance form.
    if isinstance(marker, LastValue):
        return "last_value"
    if isinstance(marker, ReducerChannel):
        return "reducer"
    return type(marker).__name__.lower()


def _find_channel_marker(
    metadata: list[Any],
) -> BaseChannel[Any] | type[BaseChannel[Any]] | None:
    """Find a channel marker in Pydantic field metadata.

    Mirrors `state._find_channel_marker` — duplicated to avoid importing a
    private symbol. Returns:
    - A `BaseChannel` instance → use as marker.
    - A `BaseChannel` subclass → use as marker (instantiated by `_fresh`).
    - `None` → no marker; defaults to `LastValue`.
    """
    for item in metadata:
        if isinstance(item, BaseChannel):
            return item
        if isinstance(item, type) and issubclass(item, BaseChannel):
            return item
    return None


class StateFactory(ABC):
    """Creates and restores `GraphState` instances. Registered by name.

    Subclasses implement:
    - `create_state() -> GraphState` — return a fresh state instance.
    - `state_schema() -> StateSchema` — return the schema describing the
      state structure (for introspection, validation, and `DynamicStateFactory`
      round-tripping).
    - `restore_state(data) -> GraphState` — restore from checkpoint data
      (the output of `GraphState.checkpoint()`).

    Registered with a `StateRegistry` under a name. A `GraphSpec` with
    `state_schema = "my_name"` references a registered factory by name.
    """

    @abstractmethod
    def create_state(self) -> GraphState:
        """Return a fresh `GraphState` instance with default field values."""
        ...

    @abstractmethod
    def state_schema(self) -> StateSchema:
        """Return the `StateSchema` describing this factory's state structure."""
        ...

    @abstractmethod
    def restore_state(self, data: dict[str, Any]) -> GraphState:
        """Restore a `GraphState` from checkpoint data.

        `data` is the output of `GraphState.checkpoint()` — a JSON-serializable
        dict. Round-trips with `create_state().checkpoint()`.
        """
        ...


class StateRegistry:
    """Registry of `StateFactory` by name.

    Usage:

    ```python
    registry = StateRegistry()
    registry.register("react_state", SimpleStateFactory(ReactState))
    registry.register("dynamic_state", DynamicStateFactory(schema))

    state = registry.create_state("react_state")
    restored = registry.restore_state("react_state", checkpoint_data)
    ```

    A `GraphSpec` with `state_schema = "react_state"` references a registered
    factory by name. `GraphSpecCompiler` (P2) resolves the name via the
    registry at compile time.
    """

    def __init__(self) -> None:
        self._factories: dict[str, StateFactory] = {}

    def register(self, name: str, factory: StateFactory) -> None:
        """Register `factory` under `name`.

        Raises:
            ValueError: if `name` is already registered.
        """
        if name in self._factories:
            raise ValueError(
                f"State factory {name!r} is already registered. "
                f"Use a different name or unregister first."
            )
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        """Remove `name` from the registry. No-op if not registered."""
        self._factories.pop(name, None)

    def create_state(self, name: str) -> GraphState:
        """Create a fresh state instance from the factory registered under `name`.

        Raises:
            KeyError: if `name` is not registered.
        """
        factory = self._factories.get(name)
        if factory is None:
            raise KeyError(
                f"State factory {name!r} is not registered. "
                f"Registered names: {sorted(self._factories.keys())}."
            )
        return factory.create_state()

    def restore_state(self, name: str, data: dict[str, Any]) -> GraphState:
        """Restore a state instance from checkpoint data.

        Raises:
            KeyError: if `name` is not registered.
        """
        factory = self._factories.get(name)
        if factory is None:
            raise KeyError(
                f"State factory {name!r} is not registered. "
                f"Registered names: {sorted(self._factories.keys())}."
            )
        return factory.restore_state(data)

    def is_registered(self, name: str) -> bool:
        """True if `name` has a registered factory."""
        return name in self._factories

    def registered_names(self) -> list[str]:
        """Sorted list of registered factory names."""
        return sorted(self._factories.keys())

    def get_factory(self, name: str) -> StateFactory | None:
        """Return the `StateFactory` registered under `name`, or None if not registered.

        Public lookup for callers that need the factory itself (not just its
        state or schema) — e.g. `GraphSpecCompiler` resolving a state_schema
        name to verify the factory exists.
        """
        return self._factories.get(name)

    def get_schema(self, name: str) -> StateSchema:
        """Return the `StateSchema` for the factory registered under `name`.

        Raises:
            KeyError: if `name` is not registered.
        """
        factory = self._factories.get(name)
        if factory is None:
            raise KeyError(
                f"State factory {name!r} is not registered. "
                f"Registered names: {sorted(self._factories.keys())}."
            )
        return factory.state_schema()


class SimpleStateFactory(StateFactory):
    """Wraps a pre-defined `GraphState` subclass. The simplest factory.

    For business code that declares `class MyState(GraphState): ...` and
    wants to register it by name. `create_state` returns `MyState()`;
    `restore_state` calls `MyState.from_checkpoint(data)`; `state_schema`
    introspects `MyState`'s fields and builds a `StateSchema` describing them.

    The introspected schema skips fields inherited from `GraphState` base
    (e.g. `resume_target`) — only business-declared fields are included.
    """

    def __init__(self, state_class: type[GraphState]) -> None:
        self._state_class = state_class

    def create_state(self) -> GraphState:
        """Return a fresh instance of the wrapped state class."""
        return self._state_class()

    def state_schema(self) -> StateSchema:
        """Introspect the wrapped state class and build a `StateSchema`.

        Reads `model_fields` for fields declared on the subclass (not
        inherited from `GraphState` base). For each field, extracts:
        - `name`: the field name.
        - `field_type`: the annotation as a string (via `_type_to_string`).
        - `channel`: the channel kind string (via `_channel_marker_to_string`).
        - `default`: the field's default value (or `None` if required).
        """
        # Skip fields inherited from GraphState base (e.g. resume_target).
        base_field_names = set(GraphState.model_fields.keys())
        own_fields = {
            name: field
            for name, field in self._state_class.model_fields.items()
            if name not in base_field_names
        }

        field_specs: list[StateFieldSpec] = []
        for name, field in own_fields.items():
            marker = _find_channel_marker(list(field.metadata))
            channel_str = _channel_marker_to_string(marker)
            type_str = _type_to_string(field.annotation)
            # Default value: use field.default if defined, else None.
            default = field.default if field.default is not PydanticUndefined else None
            field_specs.append(
                StateFieldSpec(
                    name=name,
                    field_type=type_str,
                    channel=channel_str,
                    default=default,
                )
            )

        return StateSchema(
            name=self._state_class.__name__,
            fields=field_specs,
            description=f"Introspected from {self._state_class.__name__}",
        )

    def restore_state(self, data: dict[str, Any]) -> GraphState:
        """Restore via `state_class.from_checkpoint(data)`."""
        return self._state_class.from_checkpoint(data)


class DynamicStateFactory(StateFactory):
    """Builds a `GraphState` subclass dynamically from a `StateSchema`.

    Uses `pydantic.create_model(name, __base__=GraphState, **fields)` to
    construct a class with `Annotated[T, channel_marker]` fields matching
    the `StateFieldSpec` entries. The built class is cached in `__init__`
    so repeated `create_state()` calls only pay the build cost once.

    Type resolution: `StateFieldSpec.field_type` strings are resolved via
    `_resolve_type` (safe eval with a restricted namespace — no
    `__builtins__`, only primitive types + `Any`).

    Channel resolution: `StateFieldSpec.channel` strings are resolved via
    `_resolve_channel` (`"last_value"` → `LastValue`, `"reducer"` →
    `ReducerChannel(reducer=operator.add)`).
    """

    def __init__(self, schema: StateSchema) -> None:
        self._schema = schema
        # Build the class eagerly — fails fast on bad schemas.
        self._state_class: type[GraphState] = self._build_state_class(schema)

    def _build_state_class(self, schema: StateSchema) -> type[GraphState]:
        """Build a `GraphState` subclass from the schema.

        For each `StateFieldSpec`:
        1. Resolve `field_type` string → Python type.
        2. Resolve `channel` string → channel marker.
        3. Construct `Annotated[resolved_type, channel_marker]`.
        4. Add `(annotation, default)` to the field definitions dict.

        Then call `create_model(schema.name, __base__=GraphState, **fields)`.
        """
        field_definitions: dict[str, Any] = {}
        for field_spec in schema.fields:
            resolved_type = _resolve_type(field_spec.field_type)
            channel_marker = _resolve_channel(field_spec.channel)
            # `resolved_type` is a runtime type object returned by `_resolve_type`;
            # `Annotated[resolved_type, channel_marker]` is valid at runtime but
            # not statically typeable. Typed as `Any` for mypy.
            annotation: Any = Annotated[resolved_type, channel_marker]
            field_definitions[field_spec.name] = (annotation, field_spec.default)

        return cast(
            "type[GraphState]",
            create_model(
                schema.name,
                __base__=GraphState,
                **field_definitions,
            ),
        )

    def create_state(self) -> GraphState:
        """Return a fresh instance of the dynamically built class."""
        return self._state_class()

    def state_schema(self) -> StateSchema:
        """Return the schema this factory was built from."""
        return self._schema

    def restore_state(self, data: dict[str, Any]) -> GraphState:
        """Restore via `from_checkpoint(data)` on the dynamic class."""
        return self._state_class.from_checkpoint(data)


__all__ = [
    "DynamicStateFactory",
    "SimpleStateFactory",
    "StateFactory",
    "StateRegistry",
]
