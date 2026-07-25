"""modex_graph — generalized typed graph engine.

Framework-agnostic sibling of `modex_agent`. Depends only on
`pydantic` + the Python standard library. The reverse dependency
(`modex_agent` → `modex_graph`) is required; `modex_graph` → `modex_agent`
is FORBIDDEN and enforced by `tests/architecture/test_modex_graph_isolation.py`.

Public surface (ADR-0033 acceptance criteria):

- **Graph primitives:** `Graph`, `Node`, `CompiledGraph`, `GraphEngine`,
  `GraphNode` (START/END sentinels).
- **Context + runtime:** `GraphContext`, `GraphRuntime`.
- **State + channels:** `GraphState`, `BaseChannel`, `LastValue`,
  `ReducerChannel`, `Codec`, `register_codec`.
- **Result types:** `NodeResult`, `Command`, `Task`, `DispatchEvent`.
- **Scheduler:** `Scheduler` (ABC), `LinearScheduler`, `ParallelScheduler`,
  `SchedulerKind`, `NodeInstanceStatus`, `NodeInstance`, `NodeTrigger`.
- **Exceptions:** `GraphBubbleUp`, `GraphInterrupt`, `GraphDrained`,
  `ParentCommand`, `InvalidUpdateError`, `RoutingError`, `GraphRecursionError`.

See `docs/adr/0033-generalized-graph-engine.md` for the authoritative design.
"""

from __future__ import annotations

from .channel import (
    BaseChannel,
    Codec,
    JsonValue,
    LastValue,
    ReducerChannel,
    register_codec,
)
from .checkpoint_store import (
    CheckpointData,
    CheckpointStore,
    InstanceRecord,
    MemoryCheckpointStore,
    SqliteCheckpointStore,
)
from .compiled_graph import CompiledGraph
from .conflict_detector import GenerationWriteTracker, WriteConflictDetector
from .constants import GraphNode, NodeInstanceStatus, NodeTrigger, SchedulerKind
from .context import GraphContext
from .dispatch_store import (
    DispatchStore,
    InMemoryDispatchStore,
    SqliteDispatchStore,
)
from .engine import GraphEngine
from .exceptions import (
    GraphBubbleUp,
    GraphDrained,
    GraphInterrupt,
    GraphRecursionError,
    InvalidUpdateError,
    ParentCommand,
    RoutingError,
)
from .graph import Edge, Graph
from .node import Node
from .result import Command, DispatchEvent, NodeResult, Task
from .runtime import GraphRuntime
from .scheduler import LinearScheduler, NodeInstance, ParallelScheduler, Scheduler
from .state import GraphState

__all__ = [
    # Graph primitives
    "Graph",
    "Node",
    "CompiledGraph",
    "GraphEngine",
    "GraphNode",
    "Edge",
    # Context + runtime
    "GraphContext",
    "GraphRuntime",
    # State + channels
    "GraphState",
    "BaseChannel",
    "LastValue",
    "ReducerChannel",
    "Codec",
    "register_codec",
    "JsonValue",
    # Result types
    "NodeResult",
    "Command",
    "Task",
    "DispatchEvent",
    # Dispatch persistence
    "DispatchStore",
    "InMemoryDispatchStore",
    "SqliteDispatchStore",
    # Conflict detection
    "WriteConflictDetector",
    "GenerationWriteTracker",
    # Checkpoint persistence
    "CheckpointStore",
    "CheckpointData",
    "InstanceRecord",
    "MemoryCheckpointStore",
    "SqliteCheckpointStore",
    # Scheduler
    "Scheduler",
    "LinearScheduler",
    "ParallelScheduler",
    "SchedulerKind",
    "NodeInstanceStatus",
    "NodeInstance",
    "NodeTrigger",
    # Exceptions
    "GraphBubbleUp",
    "GraphInterrupt",
    "GraphDrained",
    "ParentCommand",
    "InvalidUpdateError",
    "RoutingError",
    "GraphRecursionError",
]

__version__ = "1.0.0"
