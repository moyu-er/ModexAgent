"""Engine-recognized sentinel node names + scheduler kind enum.

`GraphNode.START` and `GraphNode.END` are `StrEnum` sentinels that mark the
graph entry and terminal points. They are NOT real nodes — no `Node` instance
is registered under these names. Edges from `GraphNode.START` to a real node
declare the entry point; edges to `GraphNode.END` declare a terminal
transition.

`SchedulerKind` is the `StrEnum` selecting which `Scheduler` implementation
`GraphEngine` delegates to. `LINEAR` (default) preserves the original
sequential execution behaviour; `PARALLEL` selects `ParallelScheduler`
(fan-out with concurrent task execution).

`NodeInstanceStatus` is the `StrEnum` state machine for `NodeInstance` under
`ParallelScheduler`: `DORMANT → PENDING → READY → RUNNING → COMPLETED`.

`NodeTrigger` is the per-node trigger mode `StrEnum` (Task 06):
`ON_ALL_PREDS` (wait for all activated predecessors) or `ON_RECEIVE`
(each dispatch creates an instance).

Per ADR-0033 D9.2: business modules use `StrEnum` for their own node names
(e.g. `ReActNode.START/LLM/TOOL/END`); the engine's `GraphNode` is distinct
and reserved for the engine-level sentinels.
"""

from __future__ import annotations

from enum import StrEnum


class GraphNode(StrEnum):
    """Engine-recognized sentinel node names.

    These are sentinels, not real nodes. The graph entry is declared via
    `add_edge(GraphNode.START, real_entry_node)`. Terminal transitions target
    `GraphNode.END`.
    """

    START = "__start__"
    END = "__end__"


class SchedulerKind(StrEnum):
    """Selects the `Scheduler` implementation `GraphEngine` delegates to.

    - `LINEAR` — sequential node execution (the original `GraphEngine` logic,
      now in `LinearScheduler`). Default; all existing graphs behave
      identically.
    - `PARALLEL` — `ParallelScheduler` (fan-out with concurrent task
      execution). Nodes must call `ctx.dispatch(target)` manually to route
      under this scheduler.
    """

    LINEAR = "linear"
    PARALLEL = "parallel"


class NodeInstanceStatus(StrEnum):
    """State machine for `NodeInstance` under `ParallelScheduler`.

    Transitions:

    - `DORMANT → PENDING` — instance created but not yet ready to execute.
    - `PENDING → READY` — instance queued for execution.
    - `READY → RUNNING` — scheduler picked up the instance for execution.
    - `RUNNING → COMPLETED` — node `execute()` returned.

    `DORMANT` is the initial status when an instance is first created. In the
    current phase (no fork isolation), instances transition `DORMANT → READY`
    immediately upon creation (no gating). `PENDING` is reserved for future
    trigger-mode gating (Task 04+).
    """

    DORMANT = "dormant"
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"


class NodeTrigger(StrEnum):
    """Per-node trigger mode under `ParallelScheduler` (Task 06).

    Controls when a node becomes READY given inbound dispatches:

    - `ON_ALL_PREDS` (default): the node waits until every "activated source"
      (a predecessor that has actually dispatched to it) has dispatched at
      least once AND no active instance can reach it via outgoing edges.
      One instance is then created consuming one dispatch per source.
    - `ON_RECEIVE`: each dispatch creates a new instance immediately.
      Reachability is NOT checked for ON_RECEIVE — the instance is marked
      READY unconditionally.
    """

    ON_ALL_PREDS = "on_all_preds"
    ON_RECEIVE = "on_receive"
