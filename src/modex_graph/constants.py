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

`NodeTrigger` is the per-node trigger mode `StrEnum`:
`ON_ALL_PREDS` (wait for all activated predecessors) or `ON_RECEIVE`
(each dispatch creates an instance).

`GraphInstanceStatus` is the lifecycle state machine `StrEnum` for
`GraphInstance`: `running` / `paused` / `stopped` /
`crashed` / `completed` / `failed`. Recovery rules: `paused` is NOT
auto-recovered (manual resume only); `stopped` is terminal (manual
termination, not resumable); `crashed` IS auto-recovered by fault
recovery; `completed`/`failed` are terminal.

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

    `DORMANT` is the initial status when an instance is first created.
    Trigger gating moves it through `PENDING` before it becomes `READY`, the
    scheduler marks it `RUNNING` during execution, and successful execution
    ends at `COMPLETED`.
    """

    DORMANT = "dormant"
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"


class NodeTrigger(StrEnum):
    """Per-node trigger mode under `ParallelScheduler`.

    Controls when a node becomes READY given inbound dispatches:

    - `ON_ALL_PREDS` (default, **stable**): the node waits until every
      "activated source" (a predecessor that has actually dispatched to it)
      has dispatched at least once AND no active instance can reach it via
      outgoing edges. One instance is then created consuming all currently
      pending dispatches from the activated sources (batch semantics:
      IntegratedInput may contain multiple payloads per source). This is
      the recommended trigger for all production graphs.
    - `ON_RECEIVE` (**deprecated / experimental**): each dispatch creates a
      new instance immediately. Reachability is NOT checked. The per-node
      FIFO serial gate is in-memory only and not persisted across crashes.
      Not recommended for new production graphs — use `ON_ALL_PREDS`.
      `Graph.compile()` emits a `DeprecationWarning` when this trigger is
      used; `GraphSpec` (declarative API) rejects it entirely.
    """

    ON_ALL_PREDS = "on_all_preds"
    ON_RECEIVE = "on_receive"


class GraphInstanceStatus(StrEnum):
    """Lifecycle state machine for `GraphInstance`.

    Transitions:

    - `running → paused` — manual pause.
    - `running → stopped` — manual stop (terminal).
    - `running → crashed` — unhandled exception / process kill.
    - `running → completed` — normal termination (terminal).
    - `running → failed` — error termination (terminal).
    - `paused → running` — manual resume.
    - `crashed → running` — fault-recovery auto-resume.

    Recovery rules:

    - `paused`: NOT auto-recovered by fault recovery. Only manual
      `resume()` transitions it back to `running`.
    - `stopped`: terminal — manual termination, NOT resumable.
    - `crashed`: auto-recovered by fault recovery (load latest checkpoint
      → rebuild scheduler state → re-dispatch).
    - `completed` / `failed`: terminal — no recovery.
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    CRASHED = "crashed"
    COMPLETED = "completed"
    FAILED = "failed"


class InvocationStatus(StrEnum):
    """Persistent status for an invocation version chain.

    Records begin directly as ``RUNNING`` (no ``PENDING`` intermediate).
    Terminal states: ``COMPLETED``, ``CANCELED``, ``CRASHED`` — no
    transition FROM terminal. Suspension no longer exists at node level:
    ``GraphInterrupt`` calls ``cancel_invocation``. Instance-level ``PAUSED``
    is a separate graph-instance store concept.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    CANCELED = "canceled"
    CRASHED = "crashed"


class DeliverConsumptionStatus(StrEnum):
    """Consumption status for delivers across persistence implementations."""

    STAGED = "staged"
    PENDING = "pending"
    CONSUMED_PENDING = "consumed_pending"
    CONSUMED_COMPLETED = "consumed_completed"


# ── Framework-injected payload source sentinels ──────────────────────────
# Used as ``IntegratedPayload.source_node`` when the payload is injected by
# the framework (resume snapshot, undelivered-retry error feedback) rather
# than by a real upstream node. StrEnum so they never collide with real
# node IDs (which are ``node_`` prefixed IDs from ``generate_id``).


class FrameworkPayloadSource(StrEnum):
    """Sentinel ``source_node`` values for framework-injected payloads.

    These are NOT real nodes — they mark payloads injected by the engine
    during resume (``RESUME``) or undelivered-retry feedback
    (``FRAMEWORK``). Using a StrEnum instead of bare strings prevents
    collision with real node IDs and satisfies type-safety rule 1
    (constants over raw strings).
    """

    RESUME = "__resume__"
    FRAMEWORK = "__framework__"
