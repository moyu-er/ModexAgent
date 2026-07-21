# ruff: noqa: ANN401

"""Channels: typed state slots with reducer semantics + codec registry.

A `BaseChannel[T]` holds a single state value of type `T` and defines how
concurrent/sequential updates fold into it:

- `LastValue[T]` — last write wins (single-writer semantics, default).
- `ReducerChannel[T](reducer)` — binary operator fan-in (e.g. `operator.add`
  for list concatenation, `set.union` for set merging).

Channels are the per-field state containers backing `GraphState`. Each field
on a `GraphState` subclass is mirrored by a channel instance; the channel
defines both update semantics (imperative mutate vs declarative
`NodeResult.state_update`) and checkpoint serialization.

Codec registry
--------------

`register_codec(python_type, codec)` registers a `Codec(encode, decode)` pair
for a non-primitive Python type. The codec is used by `channel.checkpoint()`
and `channel.restore()` to round-trip the value through `JsonValue`.

Pydantic `BaseModel` subclasses are universally supported via
`model_dump(mode="json")` / `model_validate()` — no registration needed.
Primitives (`int`, `str`, `float`, `bool`, `None`) pass through as-is.

Per ADR-0033 D14: per-type registration is the Phase-a default. Per-field
codec differentiation is deferred to Phase c (no real use case yet).
"""

from __future__ import annotations

import dataclasses
import types
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, Union, cast, get_args, get_origin

from pydantic import TypeAdapter
from typing_extensions import TypeVar

if TYPE_CHECKING:
    from pydantic import BaseModel

# JsonValue: recursive JSON-compatible value type. Defined locally to keep
# modex_graph free of modex_agent imports (modex_agent.runtime.models also
# defines JsonValue, but we cannot depend on it per ADR-0033 D11).
type JsonPrimitive = str | int | float | bool | None
type JsonValue = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]

T = TypeVar("T", default=Any)


class Codec:
    """Encode/decode pair for round-tripping a value through `JsonValue`.

    Used by channels at checkpoint time. The default codec for Pydantic
    `BaseModel` subclasses is `model_dump(mode="json")` / `model_validate()`,
    applied automatically — no `Codec` registration needed. `register_codec`
    is for non-Pydantic types that need custom serialization (e.g. third-party
    dataclasses, sets, custom containers).

    Held as a regular class (not Pydantic BaseModel) because it carries
    callable references — per rule 12, runtime objects with state/connections
    are regular classes. Frozen by convention: do not mutate `encode`/`decode`
    after construction.
    """

    __slots__ = ("encode", "decode")

    def __init__(
        self,
        encode: Callable[[Any], JsonValue],
        decode: Callable[[JsonValue], Any],
    ) -> None:
        self.encode = encode
        self.decode = decode

    def __repr__(self) -> str:
        return f"Codec(encode={self.encode!r}, decode={self.decode!r})"


# Per-type codec registry. Keyed by the exact Python type object.
# Lookup falls back to walking MRO for subclass matches.
_CODECS: dict[type, Codec] = {}


def register_codec(python_type: type, codec: Codec) -> None:
    """Register a `Codec` for `python_type`.

    Subsequent `channel.checkpoint()` / `channel.restore()` calls for values
    whose runtime type matches `python_type` (or a subclass) will use this
    codec. Pydantic `BaseModel` subclasses are handled universally and do NOT
    need registration.
    """
    _CODECS[python_type] = codec


def _find_codec(python_type: type) -> Codec | None:
    """Look up a codec by type, walking the MRO for subclass matches."""
    # Fast path: exact match.
    codec = _CODECS.get(python_type)
    if codec is not None:
        return codec
    # MRO walk for subclass matches.
    if isinstance(python_type, type):
        for cls in python_type.__mro__[1:]:
            codec = _CODECS.get(cls)
            if codec is not None:
                return codec
    return None


def _is_pydantic_model_class(cls: type) -> bool:
    """True if `cls` is a Pydantic v2 BaseModel subclass (not BaseModel itself)."""
    try:
        from pydantic import BaseModel
    except ImportError:  # pragma: no cover
        return False
    return isinstance(cls, type) and issubclass(cls, BaseModel) and cls is not BaseModel


# Stage 1 transition bridge (ADR-0034 D1): deleted in Stage 2 once the six
# value objects migrate to BaseModel. Cached to avoid repeated construction.
_TYPE_ADAPTERS: dict[type, TypeAdapter[Any]] = {}


def _get_or_create_adapter(python_type: type) -> TypeAdapter[Any]:
    """Return a cached `TypeAdapter` for `python_type`, constructing on first use."""
    adapter = _TYPE_ADAPTERS.get(python_type)
    if adapter is None:
        adapter = TypeAdapter(python_type)
        _TYPE_ADAPTERS[python_type] = adapter
    return adapter


def encode_value(value: Any) -> JsonValue:
    """Encode a Python value into a `JsonValue` for checkpoint storage.

    Order of precedence:
    1. `None` → `None`.
    2. Primitive (`str`/`int`/`float`/`bool`) → returned as-is.
    3. Pydantic `BaseModel` → `value.model_dump(mode="json")`.
    4. Stdlib `@dataclass` → `TypeAdapter(type(value)).dump_python(value, mode="json")`.
    5. Registered codec for `type(value)` → `codec.encode(value)`.
    6. `list` / `tuple` → element-wise encoding.
    7. `dict` with `str` keys → value-wise encoding.
    8. Fallback: `str(value)` (lossy; signals an unregistered non-serializable type).
    """
    if value is None:
        return None
    if isinstance(value, str | int | float | bool):
        return value
    if _is_pydantic_model_class(type(value)):
        return value.model_dump(mode="json")  # type: ignore[no-any-return]
    if dataclasses.is_dataclass(value) and not _is_pydantic_model_class(type(value)):
        adapter = _get_or_create_adapter(type(value))
        return adapter.dump_python(value, mode="json")  # type: ignore[no-any-return]
    codec = _find_codec(type(value))
    if codec is not None:
        return codec.encode(value)
    if isinstance(value, list | tuple):
        return [encode_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): encode_value(v) for k, v in value.items()}
    # Last-resort fallback. Signals the caller should register a codec.
    return str(value)


def decode_value(field_type: Any, data: JsonValue) -> Any:
    """Decode a `JsonValue` back into a Python value of `field_type`.

    `field_type` is the declared field annotation (e.g. `int`,
    `list[ChatMessage]`, `ReActNode | None`). Handles generic forms via
    `typing.get_origin` / `typing.get_args`.
    """
    origin = get_origin(field_type)
    args = get_args(field_type)
    # None / NoneType
    if field_type is type(None) or field_type is None:
        return None
    if data is None:
        return None
    # Optional[X] / Union[X, None] / X | None — strip NoneType and decode via
    # the first non-None arm. PEP 604 ``X | None`` has origin ``types.UnionType``
    # (distinct from ``typing.Union``); both arms must be handled identically.
    if origin is Union or origin is types.UnionType:
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            return decode_value(non_none_args[0], data)
        # Multi-arm union: try each until one succeeds.
        last_err: Exception | None = None
        for arm in non_none_args:
            try:
                return decode_value(arm, data)
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        if last_err is not None:
            raise last_err
        return data
    # Primitive types — coerce.
    if field_type in (str, int, float, bool):
        coerce = cast("Callable[[JsonValue], Any]", field_type)
        return coerce(data)
    # list[X] — element-wise decode.
    if origin is list:
        item_type = args[0] if args else Any
        if not isinstance(data, list):
            return data
        return [decode_value(item_type, d) for d in data]
    # dict[K, V] — value-wise decode (keys assumed str).
    if origin is dict:
        val_type = args[1] if len(args) >= 2 else Any
        if not isinstance(data, dict):
            return data
        return {k: decode_value(val_type, v) for k, v in data.items()}
    # Pydantic BaseModel subclass.
    if isinstance(field_type, type) and _is_pydantic_model_class(field_type):
        model_type = cast("type[BaseModel]", field_type)
        return model_type.model_validate(data)
    # Stdlib @dataclass subclass — route through TypeAdapter (Stage 1 bridge).
    if (
        isinstance(field_type, type)
        and dataclasses.is_dataclass(field_type)
        and not _is_pydantic_model_class(field_type)
    ):
        adapter = _get_or_create_adapter(field_type)
        return adapter.validate_python(data)
    # Enum subclass — call with the raw value.
    if isinstance(field_type, type) and issubclass(field_type, Enum):
        enum_factory = cast("Callable[[JsonValue], Any]", field_type)
        return enum_factory(data)
    # Registered codec for the field type.
    if isinstance(field_type, type):
        codec = _find_codec(field_type)
        if codec is not None:
            return codec.decode(data)
    # Fallback: return data as-is. Caller is responsible for type correctness.
    return data


class BaseChannel(ABC, Generic[T]):
    """Per-field state container with reducer semantics + checkpoint codec.

    A channel holds a single value of type `T`. Updates fold into the value
    via `update(values)` (declarative mode) or overwrite via `set(value)`
    (imperative mode sync). The value is retrieved via `get()`.

    Checkpoint serialization goes through `checkpoint()` / `restore(data)`,
    which use the codec registry (`encode_value` / `decode_value`).

    The `_field_type` attribute is set by `GraphState` at channel creation
    time, carrying the declared field annotation. It drives `restore` decoding.
    """

    _field_type: Any = Any

    @abstractmethod
    def update(self, values: list[T]) -> None:
        """Fold `values` into the channel per the channel's reducer semantics.

        Called by the engine when a node returns `NodeResult(state_update={...})`.
        For `LastValue`, the last value wins. For `ReducerChannel`, values are
        folded left-to-right via the configured reducer.
        """

    @abstractmethod
    def set(self, value: T) -> None:
        """Direct overwrite, bypassing update/reducer logic.

        Called by `GraphState._sync_fields_to_channels()` when mirroring
        imperative Pydantic field mutations into the channel prior to a
        checkpoint. Distinct from `update` so that imperative mutations
        don't trigger reducer folding.
        """

    @abstractmethod
    def get(self) -> T:
        """Return the current channel value."""

    @abstractmethod
    def checkpoint(self) -> JsonValue:
        """Encode the current value to `JsonValue` via the codec registry."""

    @abstractmethod
    def restore(self, data: JsonValue) -> None:
        """Decode `data` (via `_field_type` + codec registry) and set the value."""

    @abstractmethod
    def _fresh(self, field_type: Any) -> BaseChannel[T]:
        """Return a new empty channel of the same type with the same config.

        Used by `GraphState` at `__init__` time to create per-instance
        channels from the `Annotated[T, ChannelSpec]` marker. The marker
        instance is shared across all GraphState instances of a subclass;
        `_fresh` clones it (with config, without state) for each instance.
        """


class LastValue(BaseChannel[T]):
    """Last-write-wins channel. The default for fields without a ChannelSpec.

    Phase-a does NOT enforce single-writer semantics (no parallel execution).
    Phase-c will raise `InvalidUpdateError` when ≥2 writes happen in one
    superstep. See ADR-0033 D4.
    """

    def __init__(self) -> None:
        self._value: T | None = None

    def update(self, values: list[T]) -> None:
        if values:
            self._value = values[-1]

    def set(self, value: T) -> None:
        self._value = value

    def get(self) -> T:
        # The value is set before first read by GraphState.__init__ syncing
        # the Pydantic default into the channel, so None never occurs at
        # runtime; cast narrows T | None → T for callers.
        return cast(T, self._value)

    def checkpoint(self) -> JsonValue:
        return encode_value(self._value)

    def restore(self, data: JsonValue) -> None:
        self._value = decode_value(self._field_type, data)

    def _fresh(self, field_type: Any) -> BaseChannel[T]:
        channel = LastValue()
        channel._field_type = field_type
        return channel

    def __repr__(self) -> str:
        return f"LastValue(field_type={self._field_type!r}, value={self._value!r})"


class ReducerChannel(BaseChannel[T]):
    """Reducer/fan-in channel. Folds multiple writes via a binary operator.

    `reducer(left, right) -> combined`. The reducer is NOT required to be
    commutative; documentation states that order-sensitive reducers used with
    parallel fan-out (Phase c) produce order-dependent results.

    Example: `ReducerChannel(reducer=operator.add)` for `list` concatenation,
    `ReducerChannel(reducer=lambda a, b: a | b)` for `set` union.
    """

    def __init__(self, reducer: Callable[[T, T], T]) -> None:
        self._reducer = reducer
        self._value: T | None = None

    def update(self, values: list[T]) -> None:
        for v in values:
            if self._value is None:
                self._value = v
            else:
                self._value = self._reducer(self._value, v)

    def set(self, value: T) -> None:
        # Imperative sync: direct overwrite, bypassing reducer. This mirrors
        # LastValue.set semantics — the channel reflects the current Pydantic
        # field value exactly, not a fold of historical writes.
        self._value = value

    def get(self) -> T:
        return cast(T, self._value)

    def checkpoint(self) -> JsonValue:
        return encode_value(self._value)

    def restore(self, data: JsonValue) -> None:
        self._value = decode_value(self._field_type, data)

    def _fresh(self, field_type: Any) -> BaseChannel[T]:
        channel = ReducerChannel(reducer=self._reducer)
        channel._field_type = field_type
        return channel

    def __repr__(self) -> str:
        return (
            f"ReducerChannel(reducer={self._reducer!r}, "
            f"field_type={self._field_type!r}, value={self._value!r})"
        )


__all__ = [
    "BaseChannel",
    "Codec",
    "JsonValue",
    "LastValue",
    "ReducerChannel",
    "register_codec",
]
