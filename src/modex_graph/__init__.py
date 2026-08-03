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
- **Result types:** `NodeResult`, `DispatchEvent`.
- **Deliver/submit (ticket 07):** `IntegratedPayload`, `IntegratedInput`,
  `InputIntegrator`, `DefaultInputIntegrator`, `DeliverStore`,
  `InMemoryDeliverStore`, `SqliteDeliverStore`, `DeliverRecord`,
  `DeliverStatus`.
- **Scheduler:** `Scheduler` (ABC), `LinearScheduler`, `ParallelScheduler`,
  `SchedulerKind`, `NodeInstanceStatus`, `NodeInstance`, `NodeTrigger`.
- **Lifecycle + interrupt:** `GraphInstanceStatus` (ticket 10 class 3),
  `InterruptPolicy` (ABC, ticket 04), `CrashPolicy` (default, ticket 04).
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
from .constants import (
    GraphInstanceStatus,
    GraphNode,
    NodeInstanceStatus,
    NodeTrigger,
    SchedulerKind,
)
from .context import GraphContext
from .deliver_store import (
    DeliverRecord,
    DeliverStatus,
    DeliverStore,
    InMemoryDeliverStore,
    SqliteDeliverStore,
)
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
from .graph_instance import GraphInstance
from .id_generator import IdGenerator, SnowflakeIdGenerator, default_id_generator
from .instance_store import (
    GraphInstanceStore,
    InMemoryGraphInstanceStore,
    SqliteGraphInstanceStore,
)
from .integration import (
    DefaultInputIntegrator,
    InputIntegrator,
    IntegratedInput,
    IntegratedPayload,
)
from .interrupt_policy import CrashPolicy, InterruptPolicy
from .node import Node
from .node_factory import NodeFactory, NodeRegistry
from .node_state import NodeState, SimpleNodeState
from .node_state_store import (
    InMemoryNodeStateStore,
    NodeStateStore,
    SqliteNodeStateStore,
)
from .nodes import (
    DelayNode,
    DelayNodeFactory,
    FunctionNode,
    FunctionNodeFactory,
    GraphAsNode,
    GraphAsNodeFactory,
    HumanInputNode,
    HumanInputNodeFactory,
)
from .result import DispatchEvent, NodeResult
from .runtime import GraphRuntime
from .scheduler import LinearScheduler, NodeInstance, ParallelScheduler, Scheduler
from .spec import EdgeSpec, GraphSpec, NodeSpec
from .spec_compiler import GraphSpecCompiler
from .spec_store import (
    GraphSpecStore,
    InMemoryGraphSpecStore,
    SqliteGraphSpecStore,
)
from .state import GraphState
from .state_factory import (
    DynamicStateFactory,
    SimpleStateFactory,
    StateFactory,
    StateRegistry,
)
from .state_schema import StateFieldSpec, StateSchema
from .topology_validator import TopologyError, TopologyValidator

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
    "DispatchEvent",
    # ID generation
    "IdGenerator",
    "SnowflakeIdGenerator",
    "default_id_generator",
    # Dispatch persistence
    "DispatchStore",
    "InMemoryDispatchStore",
    "SqliteDispatchStore",
    # Deliver/submit persistence (ticket 07)
    "DeliverStatus",
    "DeliverRecord",
    "DeliverStore",
    "InMemoryDeliverStore",
    "SqliteDeliverStore",
    # Deliver/submit input integration (ticket 07)
    "IntegratedPayload",
    "IntegratedInput",
    "InputIntegrator",
    "DefaultInputIntegrator",
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
    # Lifecycle + interrupt policy (ticket 04 + ticket 10 class 3)
    "GraphInstanceStatus",
    "InterruptPolicy",
    "CrashPolicy",
    # Exceptions
    "GraphBubbleUp",
    "GraphInterrupt",
    "GraphDrained",
    "ParentCommand",
    "InvalidUpdateError",
    "RoutingError",
    "GraphRecursionError",
    # Declarative graph spec (ticket 08)
    "GraphSpec",
    "NodeSpec",
    "EdgeSpec",
    # Runtime graph instance (ticket 04)
    "GraphInstance",
    # Graph spec persistence (P1C.6)
    "GraphSpecStore",
    "InMemoryGraphSpecStore",
    "SqliteGraphSpecStore",
    # Graph instance persistence (P1C.6)
    "GraphInstanceStore",
    "InMemoryGraphInstanceStore",
    "SqliteGraphInstanceStore",
    # Node state persistence (P1C.6)
    "NodeStateStore",
    "InMemoryNodeStateStore",
    "SqliteNodeStateStore",
    # Node state in-memory abstraction (ticket 10)
    "NodeState",
    "SimpleNodeState",
    "StateSchema",
    "StateFieldSpec",
    "NodeFactory",
    "NodeRegistry",
    "StateFactory",
    "StateRegistry",
    "SimpleStateFactory",
    "DynamicStateFactory",
    # Generic Node types + factories (ticket 02)
    "FunctionNode",
    "FunctionNodeFactory",
    "GraphAsNode",
    "GraphAsNodeFactory",
    "DelayNode",
    "DelayNodeFactory",
    "HumanInputNode",
    "HumanInputNodeFactory",
    # Declarative → imperative bridge (ticket 08)
    "GraphSpecCompiler",
    "TopologyValidator",
    "TopologyError",
]

__version__ = "1.0.0"
