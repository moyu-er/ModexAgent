# ruff: noqa: ANN401
"""Structured node return values: `NodeResult` + `DispatchEvent`.

A node's `execute(ctx)` returns a `NodeResult` — a frozen Pydantic value
object carrying:

- `state_update: dict[str, Any] | None` — declarative state mutation. The
  engine calls `channel.update([value])` for each entry, then syncs back to
  the Pydantic field. Bypassed when None.

Routing is deliver-only: nodes call `self.deliver(content, next_node, ctx)`
during `execute()` to accumulate delivers, and the framework dispatches them
via `_submit` after `execute` returns. `NodeResult` carries no routing fields
— the legacy `transition` / `command` / `Task` / `Command` types were removed
as dead code (P3.4b convergence).

`DispatchEvent` is the immutable record of a single `ctx.dispatch()` call
under `ParallelScheduler`. It is a frozen Pydantic value object (per rules
10–16) carrying the source instance ID, the target node name (or
`GraphNode.END`), and an optional payload. The `ParallelScheduler` appends
one `DispatchEvent` per dispatch call to its `dispatch_log` and uses them
for audit / debugging.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class NodeResult(BaseModel):
    """Structured return value from `Node.execute(ctx)`.

    Single field, optional:

    - `state_update` — declarative state mutation. Applied by the engine
      before routing is resolved (routing is deliver-only).

    Routing is handled entirely by `deliver()` / `_submit()` — `NodeResult`
    itself carries no routing information.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state_update: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Declarative state mutation. Engine calls "
            "`channel.update([value])` for each entry, then syncs back to "
            "the Pydantic field. Open payload keyed by field name."
        ),
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


__all__ = ["NodeResult", "DispatchEvent"]
