"""`GraphInstance` — runtime graph instance abstraction (ticket 04).

A `GraphInstance` is the persistence/recovery unit. It is created when a
`GraphSpec` is compiled and instantiated, and carries the
`graph_instance_id` (a Snowflake ID — the persistence unique key that
replaces the in-memory `run_id`) plus parent linkage for nested subgraphs.

The full chain (per ticket 08 / `spec.py`):

    GraphSpec → GraphSpecCompiler → CompiledGraph → GraphInstance → GraphEngine

Design note — why `GraphInstance` carries IDs only, not runtime objects:

The ticket lists `graph_spec` and `compiled_graph` as conceptual attributes
of a graph instance. In the implementation they are NOT fields on this
frozen model:

- `GraphSpec` is a frozen Pydantic model, but persisting the full spec on
  every instance row duplicates data — the `graph_specs` table (P0.2) is
  the single source of truth for spec content. `GraphInstance.spec_id`
  links to it.
- `CompiledGraph` is a `@dataclass(frozen=True)` holding runtime `Node`
  objects (closures, callables) — it cannot be serialized. It is built
  fresh by the `GraphSpecCompiler` at runtime.

The bot factory (P3.5) loads the `GraphSpec` from `spec_id`, compiles it
to get a `CompiledGraph`, then pairs them with the `GraphInstance` in
memory. `GraphInstance` itself only persists the identity + status —
that is the contract of a recovery unit.

`graph_instance_id` is a Snowflake-format `int` (per `id_generator.py`),
matching the `BIGINT` column in the P0.2 DDL. The PRD text says `str`; the
implementation deliberately uses `int` because Snowflake IDs are 64-bit
signed ints and SQLite stores them natively as `INTEGER`. This is the
single persistence key (rule 15: converge — replaces `run_id`).

Lifecycle status uses the `GraphInstanceStatus` StrEnum
(running/paused/stopped/crashed/completed/failed). The DB schema (P0.2)
enforces a CHECK constraint on the string value. `StrEnum` is a `str`
subclass, so existing callers passing `.value` or raw strings continue
to work.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .constants import GraphInstanceStatus

__all__ = ["GraphInstance"]


class GraphInstance(BaseModel):
    """Runtime graph instance — the persistence/recovery unit (ticket 04).

    A `GraphInstance` is created when a `GraphSpec` is compiled and
    instantiated. It carries the `graph_instance_id` (Snowflake, the
    persistence unique key that replaces `run_id`), parent linkage (for
    nested subgraphs), and runtime status.

    This is a data structure, NOT a runtime engine. It holds only IDs +
    status. The `GraphSpec` (loaded from `spec_id` via the `graph_specs`
    table) and `CompiledGraph` (built fresh by the `GraphSpecCompiler`)
    are paired with the instance at runtime by the bot factory (P3.5).

    Persistence: all graph instances (outer + nested) live in one table
    (`graph_instances`), distinguished by `graph_instance_id` and linked
    via `parent_instance_id` (ticket 04 — unified schema).

    Fields:
    - `graph_instance_id: int` — Snowflake ID, the persistence unique key.
      Replaces the in-memory `run_id` (rule 15: converge on a single key).
    - `spec_id: int` — FK → `graph_specs.spec_id`; the `GraphSpec` that
      defines this instance.
    - `parent_instance_id: int | None` — parent graph instance when this
      instance is a nested subgraph; `None` for the outer instance.
    - `parent_node: str | None` — node name in the parent graph that
      created this instance; `None` for the outer instance.
    - `status: GraphInstanceStatus` — lifecycle status (StrEnum). The
      DB schema enforces a CHECK constraint on the allowed values.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_instance_id: int = Field(
        description=(
            "Snowflake ID — the persistence unique key for this graph "
            "instance (replaces run_id). 64-bit signed int, matches the "
            "BIGINT column in graph_instances (P0.2 DDL)."
        ),
    )
    spec_id: int = Field(
        description=(
            "FK → graph_specs.spec_id. The GraphSpec that defines this "
            "instance. The spec content is loaded from the graph_specs "
            "table at runtime, not stored on this model."
        ),
    )
    parent_instance_id: int | None = Field(
        default=None,
        description=(
            "Parent graph instance ID when this instance is a nested "
            "subgraph (recursive nesting). None for the outer instance."
        ),
    )
    parent_node: str | None = Field(
        default=None,
        description=(
            "Node name in the parent graph that created this instance. None for the outer instance."
        ),
    )
    status: GraphInstanceStatus = Field(
        default=GraphInstanceStatus.RUNNING,
        description=(
            "Lifecycle status (GraphInstanceStatus enum). "
            "The DB schema (P0.2 DDL) enforces a CHECK constraint on the "
            "string value."
        ),
    )
