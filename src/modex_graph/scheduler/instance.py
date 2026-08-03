"""``NodeInstance[S]`` — a single execution instance of a graph node.

Used by ``ParallelScheduler`` for continuous multi-instance execution. Regular
class (NOT Pydantic) per rule 12 — holds mutable runtime state. Uses
``__slots__`` for memory efficiency (one instance per node execution).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..constants import NodeInstanceStatus

if TYPE_CHECKING:
    from ..integration import IntegratedPayload
    from ..state import GraphState


class NodeInstance[S: "GraphState"]:
    """A single execution instance of a graph node under `ParallelScheduler`.

    Regular class (NOT Pydantic) per rule 12 — holds mutable runtime state.
    Uses `__slots__` for memory efficiency (one instance per node execution).

    Each call to `ParallelScheduler.run_async` creates fresh instances. The
    `instance_id` is globally unique within a single run: `{node_name}#{seq}`
    where `seq` is the scheduler's global instance counter.

    Fields:

    - `instance_id: str` — unique ID, format `{node_name}#{seq}`.
    - `node_name: str` — the graph node this instance executes.
    - `seq: int` — the global instance sequence number at creation time.
    - `status: NodeInstanceStatus` — current state-machine position.
    - `forked_state: S | None` — isolated state for this instance. `None`
      means "share `main_state`" (the fast path — no fork). A non-None
      value means the instance operates on its own state copy
      (fork-isolation path).
    - `fork_version: int` — the `main_state` version this instance forked
      from. Instances forked from the same version share a generation;
      two instances in the same generation writing the same `LastValue`
      field = conflict. `0` means the fast path (no fork, no conflict
      detection).
    - `upstream_payloads: list[IntegratedPayload] | None` — the delivered
      payloads from upstream nodes, stored on the instance for scheduler
      internal bookkeeping.       No longer passed to ``node.run()``
      — upstream payloads now flow through the coordinator's
      ``collect_consumable_delivers``. Retained for dispatch
      handler → ``coordinator.route_deliver`` wiring. `None` for the entry
      node (no upstream). Populated by ``_handle_dispatch`` /
      ``_try_fire_on_all_preds`` from dispatch
      ``state_update={"delivered": content}`` payloads.
    """

    __slots__ = (
        "instance_id",
        "node_name",
        "seq",
        "status",
        "forked_state",
        "fork_version",
        "upstream_payloads",
    )

    def __init__(
        self,
        *,
        instance_id: str,
        node_name: str,
        seq: int,
        status: NodeInstanceStatus,
        forked_state: S | None = None,
        fork_version: int = 0,
        upstream_payloads: list[IntegratedPayload] | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.node_name = node_name
        self.seq = seq
        self.status = status
        self.forked_state = forked_state
        self.fork_version = fork_version
        self.upstream_payloads = upstream_payloads

    def __repr__(self) -> str:
        return f"NodeInstance(instance_id={self.instance_id!r}, status={self.status!r})"


__all__ = ["NodeInstance"]
