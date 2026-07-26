# ruff: noqa: ANN401
"""Structured node return values: `NodeResult` + `Command` + `Task` + `DispatchEvent`.

A node's `execute(ctx)` returns a `NodeResult` — a frozen Pydantic value
object carrying:

- `transition: str | None` — static edge lookup key. The engine finds the
  next node via `add_edge(source, target, reason=transition)`. `None` means
  no static transition; the engine falls through to the default edge.
- `state_update: dict[str, Any] | None` — declarative state mutation. The
  engine calls `channel.update([value])` for each entry, then syncs back to
  the Pydantic field. Bypassed when None.
- `command: Command | None` — dynamic routing / fan-out. Highest priority.

`Command.goto` accepts three forms (two-layer routing model):

- `None` — no goto; fall through to transition / default.
- `str` — dynamic routing to one node.
- `list[Task]` — fan-out. `LinearScheduler` executes tasks sequentially;
  `ParallelScheduler` executes them concurrently (ADR-0034).

`list[str]` sequential multi-target was removed in the two-layer cleanup;
use `list[Task]` for fan-out or `str` for single-target routing.

`Task(node, state)` carries an independent state for fan-out.
`LinearScheduler` executes tasks sequentially; `ParallelScheduler`
executes them concurrently (ADR-0034 D2/D7).

`DispatchEvent` is the immutable record of a single `ctx.dispatch()` call
under `ParallelScheduler`. It is a frozen Pydantic value object (per rules
10–16) carrying the source instance ID, the target node name (or
`GraphNode.END`), and an optional payload. The `ParallelScheduler` appends
one `DispatchEvent` per dispatch call to its `dispatch_log` and uses them
for audit / debugging.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Task(BaseModel):
    """A single fan-out task — execute `node` with `state`.

    `LinearScheduler` executes tasks sequentially; `ParallelScheduler`
    executes them concurrently (ADR-0034). The upgrade is engine-only —
    node code returning `Command(goto=[Task(...)])` runs in parallel
    automatically under `ParallelScheduler`.

    `state=None` means share the parent state (mutations propagate directly).
    A non-None `state` means independent state (imperative mutations do NOT
    propagate; only `NodeResult.state_update` merges via reducer).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str = Field(description="Target node name to execute.")
    state: Any | None = Field(
        default=None,
        description=(
            "Independent state for this task. None = share parent state "
            "(mutations propagate). Non-None = isolated state."
        ),
    )


class Command(BaseModel):
    """Dynamic routing / fan-out instruction. Highest-priority routing mechanism.

    `goto` accepts:
    - `None` — no goto; fall through to transition / default.
    - `str` — jump to one node.
    - `list[Task]` — fan-out with independent state per task.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    goto: str | list[Task] | None = Field(
        default=None,
        description="Dynamic routing target(s). See class docstring for forms.",
    )

    @field_validator("goto", mode="before")
    @classmethod
    def _reject_str_list(cls, v: Any) -> Any:
        if isinstance(v, list) and len(v) > 0 and isinstance(v[0], str):
            raise ValueError(
                "Command.goto no longer accepts list[str] (removed in the "
                "two-layer routing cleanup). Use str for single-target "
                "routing or list[Task] for fan-out."
            )
        return v


class NodeResult(BaseModel):
    """Structured return value from `Node.execute(ctx)`.

    Three fields, all optional, evaluated by the engine in strict priority:

    1. `command` (if not None and `command.goto` is not None) → use goto.
    2. `transition` (if not None) → static edge lookup.
    3. default edge (reason=None) if defined.
    4. else raise `RoutingError`.

    `state_update` is applied regardless of routing — it merges into the
    channels before routing is resolved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    transition: str | None = Field(
        default=None,
        description=(
            "Static edge lookup key. The engine finds the next node via "
            "`add_edge(current, target, reason=transition)`. None = no "
            "static transition; fall through to default."
        ),
    )
    state_update: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Declarative state mutation. Engine calls "
            "`channel.update([value])` for each entry, then syncs back to "
            "the Pydantic field. Open payload keyed by field name."
        ),
    )
    command: Command | None = Field(
        default=None,
        description="Dynamic routing / fan-out. Highest priority.",
    )


class DispatchEvent(BaseModel):
    """Immutable record of a single `ctx.dispatch()` call under `ParallelScheduler`.

    Created by the `ParallelScheduler`'s dispatch handler when a node calls
    `ctx.dispatch(target, state_update)`. The `state_update` dict becomes the
    `payload` of this event. Each dispatch appends one `DispatchEvent` to the
    scheduler's `dispatch_log`.

    Frozen Pydantic model per rules 10–16: cross-module internal data
    structures MUST be `BaseModel`. The event is immutable so it can be safely
    shared across logs and audit trails.

    Fields:

    - `source_instance: str` — the `NodeInstance.instance_id` that issued the
      dispatch (e.g. `"llm#0"`). Format: `{node_name}#{seq}`.
    - `target: str` — the target node name, or `GraphNode.END` for the
      terminal signal.
    - `payload: dict[str, Any] | None` — the `state_update` passed to
      `ctx.dispatch()`. Carries data to be applied to the target instance's
      state (in future fork-isolation phases). `None` means no payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_instance: str = Field(
        description=(
            "The `NodeInstance.instance_id` that issued the dispatch. Format: `{node_name}#{seq}`."
        ),
    )
    target: str = Field(
        description=("The target node name, or `GraphNode.END` for the terminal signal."),
    )
    payload: dict[str, Any] | None = Field(
        default=None,
        description=(
            "The `state_update` passed to `ctx.dispatch()`. Carries data "
            "for the target instance. None means no payload."
        ),
    )


__all__ = ["Command", "NodeResult", "Task", "DispatchEvent"]
