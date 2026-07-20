"""Structured node return values: `NodeResult` + `Command` + `Task`.

A node's `execute(ctx)` returns a `NodeResult` — a frozen Pydantic value
object carrying:

- `transition: str | None` — static edge lookup key. The engine finds the
  next node via `add_edge(source, target, reason=transition)`. `None` means
  no static transition; the engine falls through to conditional/default edges.
- `state_update: dict[str, Any] | None` — declarative state mutation. The
  engine calls `channel.update([value])` for each entry, then syncs back to
  the Pydantic field. Bypassed when None.
- `command: Command | None` — dynamic routing / fan-out. Highest priority.

`Command.goto` accepts four forms (per ADR-0033 D6):

- `None` — no goto; fall through to transition / conditional / default.
- `str` — dynamic routing to one node.
- `list[str]` — sequential multi-target (visit each in order).
- `list[Task]` — sequential fan-out (Phase a) / parallel fan-out (Phase c).

`Task(node, state)` carries an independent state for fan-out. Phase-a
executes tasks sequentially; Phase-c will execute them in parallel.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Task(BaseModel):
    """A single fan-out task — execute `node` with `state`.

    Phase-a: sequential execution. Phase-c: parallel execution.
    The Phase-c upgrade is engine-only — node code returning
    `Command(goto=[Task(...)])` runs in parallel automatically.

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
    - `None` — no goto; fall through to transition / conditional / default.
    - `str` — jump to one node.
    - `list[str]` — sequential multi-target.
    - `list[Task]` — fan-out with independent state per task.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    goto: str | list[str] | list[Task] | None = Field(
        default=None,
        description="Dynamic routing target(s). See class docstring for forms.",
    )


class NodeResult(BaseModel):
    """Structured return value from `Node.execute(ctx)`.

    Three fields, all optional, evaluated by the engine in strict priority:

    1. `command` (if not None and `command.goto` is not None) → use goto.
    2. `transition` (if not None) → static edge lookup.
    3. conditional edge (`route_fn(state)`) if defined for the current node.
    4. default edge (reason=None) if defined.
    5. else raise `RoutingError`.

    `state_update` is applied regardless of routing — it merges into the
    channels before routing is resolved.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    transition: str | None = Field(
        default=None,
        description=(
            "Static edge lookup key. The engine finds the next node via "
            "`add_edge(current, target, reason=transition)`. None = no "
            "static transition; fall through to conditional/default."
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


__all__ = ["Command", "NodeResult", "Task"]
