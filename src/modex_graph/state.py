"""`GraphState(BaseModel)` — Pydantic state with per-field channel declaration.

Per ADR-0033 D4: state is a Pydantic `BaseModel` subclass. Each field is
annotated with `Annotated[T, ChannelSpec]`; the spec selects the channel
type. Fields without annotation default to `LastValue`.

Dual-mode state access (Z-style):

- **Imperative:** `ctx.state.iteration += 1` mutates the Pydantic field
  directly. Bypasses `channel.update`. Snapshot syncs Pydantic fields →
  channels before checkpoint.
- **Declarative:** `return NodeResult(state_update={"x": v})` — engine
  calls `channel.update([v])`, then syncs back to the Pydantic field.

Both modes coexist. The engine reconciles them via `_sync_fields_to_channels`
(imperative → channel) and `_sync_channels_to_fields` (declarative → field).

Channel declaration via `Annotated`:

```python
class MyState(GraphState):
    count: Annotated[int, LastValue] = 0
    items: Annotated[list[str], ReducerChannel(reducer=operator.add)] = []
```

`LastValue` may be used as a class (the default) or an instance. Marker
instances (`ReducerChannel(reducer=...)`) are cloned per GraphState instance
via `_fresh(field_type)` so state is never shared across instances.

Checkpoint round-trip:

- `state.checkpoint() -> dict[str, JsonValue]` — per-field channel encode.
- `GraphState.from_checkpoint(data) -> Self` — per-field channel decode +
  Pydantic construction.

This replaces ~230 lines of hand-written payload flattening (the old
`ReActSnapshotPolicy._build_payload`) with declarative per-channel codec.
"""

from __future__ import annotations

from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator

from .channel import BaseChannel, LastValue


def _find_channel_marker(metadata: list[Any]) -> BaseChannel[Any] | type[BaseChannel[Any]] | None:
    """Find a channel marker in Pydantic field metadata.

    Returns:
    - A `BaseChannel` instance (e.g. `ReducerChannel(reducer=...)`) → use as marker.
    - A `BaseChannel` subclass (e.g. `LastValue` class itself) → instantiate.
    - None → no marker; default to `LastValue`.
    """
    for item in metadata:
        if isinstance(item, BaseChannel):
            return item
        if isinstance(item, type) and issubclass(item, BaseChannel):
            return item
    return None


class GraphState(BaseModel):
    """Pydantic state base with per-field channel declaration + checkpoint.

    Subclasses declare fields with `Annotated[T, ChannelSpec]` where
    `ChannelSpec` is a `BaseChannel` instance (`LastValue()`,
    `ReducerChannel(reducer=...)`) or class (`LastValue`). Fields without
    annotation default to `LastValue`.

    The model is NOT frozen — imperative mutation (`ctx.state.x = y`) is
    allowed per D4 Z-style dual-mode. Individual value-object fields may be
    frozen Pydantic models per rule 12; `GraphState` itself is mutable.

    `_channels: dict[str, BaseChannel]` is a `PrivateAttr` populated by
    a `model_validator(mode='after')` at construction time. It mirrors the
    Pydantic fields: each field has a corresponding channel instance.

    Checkpoint serialization goes through `channel.checkpoint()` /
    `channel.restore(data)`, which use the codec registry. Pydantic
    `BaseModel` subclasses are universally supported via `model_dump` /
    `model_validate`; primitives pass through; custom types via
    `register_codec`.
    """

    model_config = ConfigDict(
        # Mutable: imperative `ctx.state.x = y` is allowed (D4 Z-style).
        # Individual value-object fields may be frozen Pydantic models.
        frozen=False,
        # Strict: extra fields are errors. Subclasses declare all state.
        extra="forbid",
        # Validate on assignment so imperative mutations run validators.
        validate_assignment=False,
        # Allow arbitrary types for fields holding non-Pydantic runtime
        # objects (e.g. LLMResponse, ToolResult). Codec registration handles
        # serialization; Pydantic does not need to validate them.
        arbitrary_types_allowed=True,
    )

    # Resume target set by ``ctx.interrupt(value, resume_to=...)``; the
    # entry node routes via ``Command(goto=...)`` on re-entry, then clears
    # it. Replaces entry-node phase hardcoding.
    resume_target: Annotated[str | None, LastValue] = None

    # Per-instance channel bag. Populated by _setup_channels model_validator.
    # Named `_channels` (single underscore) because Pydantic v2 forbids dunder
    # names for PrivateAttr. Subclasses access it as `self._channels`.
    _channels: dict[str, BaseChannel[Any]] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _setup_channels(self) -> Self:
        """Populate `_channels` from `Annotated[T, ChannelSpec]` metadata.

        For each declared field, find the channel marker in the field's
        metadata. If found, clone it via `_fresh(field_type)`. If not found,
        default to `LastValue`. Then sync the Pydantic default value into the
        channel so `get()` returns the correct initial value.
        """
        channels: dict[str, BaseChannel[Any]] = {}
        for name, field in type(self).model_fields.items():
            marker = _find_channel_marker(list(field.metadata))
            field_type = field.annotation
            if marker is None:
                # Default: LastValue. Use the class form.
                channel: BaseChannel[Any] = LastValue()._fresh(field_type)
            elif isinstance(marker, type):
                # Class form (e.g. `Annotated[T, LastValue]`). Instantiate.
                channel = marker()._fresh(field_type)
            else:
                # Instance form (e.g. `Annotated[T, ReducerChannel(reducer=op.add)]`).
                channel = marker._fresh(field_type)
            # Sync the Pydantic default value into the channel.
            current_value = getattr(self, name)
            channel.set(current_value)
            channels[name] = channel
        # Use object.__setattr__ to bypass any Pydantic assignment validation.
        object.__setattr__(self, "_channels", channels)
        return self

    def _sync_fields_to_channels(self) -> None:
        """Mirror imperative Pydantic field mutations into the channels.

        Called by `checkpoint()` before encoding, so that imperative mutations
        (`ctx.state.x = y`) are reflected in the channel values. Uses
        `channel.set(value)` (direct overwrite, bypasses reducer).
        """
        for name, channel in self._channels.items():
            current_value = getattr(self, name)
            channel.set(current_value)

    def _sync_channels_to_fields(self) -> None:
        """Mirror declarative channel updates back into the Pydantic fields.

        Called by the engine after applying `NodeResult.state_update`, so that
        `ctx.state.x` reflects the new value. Uses `object.__setattr__` to
        bypass assignment validation (the channel value is already validated).
        """
        for name, channel in self._channels.items():
            object.__setattr__(self, name, channel.get())

    def apply_state_update(self, updates: dict[str, Any]) -> None:
        """Apply a declarative `state_update` dict via the per-field channels.

        For each `(name, value)` entry, call `channel.update([value])` (which
        folds per the channel's reducer: last-write-wins for `LastValue`,
        binary fold for `ReducerChannel`). Then sync channels → fields.

        Unknown field names raise `KeyError` — consistent with `extra='forbid'`
        on the model config.
        """
        for name, value in updates.items():
            channel = self._channels.get(name)
            if channel is None:
                raise KeyError(
                    f"Unknown state field {name!r} in state_update. "
                    f"Valid fields: {sorted(self._channels.keys())}"
                )
            channel.update([value])
        self._sync_channels_to_fields()

    def checkpoint(self) -> dict[str, Any]:
        """Serialize state to a `dict[str, JsonValue]` via per-field channels.

        Syncs Pydantic fields → channels first (so imperative mutations are
        reflected), then encodes each channel value via the codec registry.

        The returned dict is JSON-serializable and can be persisted directly.
        Round-trips through `from_checkpoint(data)`.
        """
        self._sync_fields_to_channels()
        return {name: channel.checkpoint() for name, channel in self._channels.items()}

    @classmethod
    def from_checkpoint(cls, data: dict[str, Any]) -> Self:
        """Reconstruct a state instance from a checkpoint dict.

        Creates a default instance, then restores each channel from the data
        dict, then syncs channels → fields. Round-trips with `checkpoint()`.
        """
        # Construct with default values. Pydantic requires all required fields
        # to have defaults or be passed here. GraphState subclasses should
        # provide defaults for all fields (or use Field(default_factory=...)).
        instance = cls()
        for name, channel in instance._channels.items():
            if name in data:
                channel.restore(data[name])
        instance._sync_channels_to_fields()
        return instance


__all__ = ["GraphState"]
