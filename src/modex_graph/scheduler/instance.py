"""``NodeInstance[S]`` — a single execution instance of a graph node.

Used by ``ParallelScheduler`` for continuous multi-instance execution. Regular
class (NOT Pydantic) per rule 12 — holds mutable runtime state. Uses
``__slots__`` for memory efficiency (one instance per node execution).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..constants import NodeInstanceStatus

if TYPE_CHECKING:
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
    """

    __slots__ = (
        "instance_id",
        "node_name",
        "seq",
        "status",
    )

    def __init__(
        self,
        *,
        instance_id: str,
        node_name: str,
        seq: int,
        status: NodeInstanceStatus,
    ) -> None:
        self.instance_id = instance_id
        self.node_name = node_name
        self.seq = seq
        self.status = status

    def __repr__(self) -> str:
        return f"NodeInstance(instance_id={self.instance_id!r}, status={self.status!r})"


__all__ = ["NodeInstance"]
