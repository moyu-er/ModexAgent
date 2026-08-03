# ruff: noqa: ANN401
"""`StateSchema` + `StateFieldSpec` — serializable description of a `GraphState`.

Per ticket 08: state structure is described declaratively as a `StateSchema`
(a frozen Pydantic model) so it can be persisted alongside a `GraphSpec` and
round-tripped through JSON. Two reference modes (ADR-0033 D9.1 / ticket 08):

- **Inline:** `GraphSpec.state_schema = StateSchema(fields=[...])` — the
  schema is embedded directly in the spec.
- **Registered:** `GraphSpec.state_schema = "my_state_schema"` — a string
  name resolved against a `StateRegistry` at compile time.

`DynamicStateFactory` consumes a `StateSchema` to build a `GraphState`
subclass at runtime (see `state_factory.py`).

The structures here are pure data — no behavior, no I/O, no engine wiring.
They are the persistence-time shape of state; the runtime shape is a
`GraphState` Pydantic subclass with `Annotated[T, ChannelSpec]` fields.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StateFieldSpec(BaseModel):
    """Serializable description of one `GraphState` field.

    Mirrors the runtime declaration `name: Annotated[T, ChannelSpec] = default`.

    Fields:
    - `name`: the field name on the state class.
    - `field_type`: Python type as a string. Supported forms:
      `"int"`, `"str"`, `"float"`, `"bool"`, `"list"`, `"list[str]"`,
      `"dict"`, `"dict[str, Any]"`, or any string resolvable by
      `DynamicStateFactory`'s type registry. Custom types must be registered
      with the factory, not encoded inline.
    - `channel`: channel kind. `"last_value"` (default) → `LastValue`;
      `"reducer"` → `ReducerChannel(reducer=operator.add)`; or a registered
      channel name (future extension).
    - `default`: JSON-serializable default value. `None` is the sentinel for
      "no default provided" — the runtime field will default to `None`.
    - `description`: optional human-readable note.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    field_type: str
    channel: str = "last_value"
    default: Any = None
    description: str | None = None


class StateSchema(BaseModel):
    """Serializable description of a `GraphState` structure — the "shape" of state.

    A `StateSchema` is the persistence-time description of a state class. It
    lists the fields (`StateFieldSpec`) and names the schema for registry
    lookup. It does NOT carry runtime values — those live on `GraphState`
    instances.

    Two reference modes (ticket 08):

    - **Inline:** `GraphSpec.state_schema = StateSchema(name="react_state",
      fields=[...])` — schema embedded in the spec, persisted with it.
    - **Registered:** `GraphSpec.state_schema = "react_state"` — name
      resolved against a `StateRegistry` at compile time. The registry holds
      a `StateFactory` (which exposes its schema via `state_schema()`).

    `DynamicStateFactory` consumes a `StateSchema` to build a `GraphState`
    subclass dynamically via `pydantic.create_model`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    fields: list[StateFieldSpec] = Field(default_factory=list)
    description: str | None = None


__all__ = ["StateFieldSpec", "StateSchema"]
