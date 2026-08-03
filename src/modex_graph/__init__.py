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
- **Deliver/submit:** `IntegratedPayload`, `IntegratedInput`,
  `InputIntegrator`, `DefaultInputIntegrator`, `DeliverStore`,
  `InMemoryDeliverStore`, `SqliteDeliverStore`, `DeliverRecord`,
  `DeliverStatus`.
- **Scheduler:** `Scheduler` (ABC), `LinearScheduler`, `ParallelScheduler`,
  `SchedulerKind`, `NodeInstanceStatus`, `NodeInstance`, `NodeTrigger`.
- **Lifecycle + interrupt:** `GraphInstanceStatus`,
  `InterruptPolicy` (ABC), `CrashPolicy` (default).
- **Exceptions:** `GraphBubbleUp`, `GraphInterrupt`, `GraphDrained`,
  `ParentCommand`, `InvalidUpdateError`, `RoutingError`, `GraphRecursionError`.

See `docs/adr/0033-generalized-graph-engine.md` for the authoritative design.
"""

from __future__ import annotations

from .compiled_graph import CompiledGraph
from .conflict_detector import GenerationWriteTracker, WriteConflictDetector
from .constants import (
    DeliverConsumptionStatus,
    GraphInstanceStatus,
    GraphNode,
    InvocationStatus,
    NodeInstanceStatus,
    NodeTrigger,
    SchedulerInstanceStatus,
    SchedulerKind,
)
from .context import GraphContext
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
from .id_generator import IdGenerator, SnowflakeIdGenerator, default_id_generator
from .integration import (
    DefaultInputIntegrator,
    InputIntegrator,
    IntegratedInput,
    IntegratedPayload,
)
from .interrupt_policy import CrashPolicy, InterruptPolicy
from .node import Node
from .node_factory import NodeFactory, NodeRegistry
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
from .persistence import (
    DeliverRecord,
    DeliverStatus,
    DeliverStore,
    DeliverStoreFactory,
    DispatchStore,
    GraphInstance,
    GraphInstanceStore,
    GraphMetadata,
    GraphMetadataStore,
    GraphPersistenceCoordinator,
    GraphStateSnapshot,
    InMemoryDeliverStore,
    InMemoryDeliverStoreFactory,
    InMemoryDispatchStore,
    InMemoryGraphInstanceStore,
    InMemoryNodeStateStore,
    InvocationContext,
    MemoryGraphMetadataStore,
    NodeInvocationRecord,
    NodeState,
    NodeStateFactory,
    NodeStateStore,
    NullDeliverStore,
    NullDeliverStoreFactory,
    NullGraphMetadataStore,
    NullNodeState,
    NullNodeStateFactory,
    RecoveryContext,
    SimpleNodeState,
    SimpleNodeStateFactory,
    SqliteDeliverStore,
    SqliteDeliverStoreFactory,
    SqliteDispatchStore,
    SqliteGraphInstanceStore,
    SqliteGraphMetadataStore,
    SqliteNodeState,
    SqliteNodeStateFactory,
    SqliteNodeStateStore,
    create_null_coordinator,
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
from .state import (
    BaseChannel,
    Codec,
    DynamicStateFactory,
    GraphState,
    JsonValue,
    LastValue,
    ReducerChannel,
    SimpleStateFactory,
    StateFactory,
    StateFieldSpec,
    StateRegistry,
    StateSchema,
    register_codec,
)
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
    # Deliver/submit persistence
    "DeliverStatus",
    "DeliverRecord",
    "DeliverStore",
    "DeliverStoreFactory",
    "InMemoryDeliverStore",
    "InMemoryDeliverStoreFactory",
    "NullDeliverStore",
    "NullDeliverStoreFactory",
    "SqliteDeliverStore",
    "SqliteDeliverStoreFactory",
    # Deliver/submit input integration
    "IntegratedPayload",
    "IntegratedInput",
    "InputIntegrator",
    "DefaultInputIntegrator",
    # Conflict detection
    "WriteConflictDetector",
    "GenerationWriteTracker",
    # Scheduler
    "Scheduler",
    "LinearScheduler",
    "ParallelScheduler",
    "SchedulerKind",
    "SchedulerInstanceStatus",
    "NodeInstanceStatus",
    "NodeInstance",
    "NodeTrigger",
    # Lifecycle + interrupt policy
    "GraphInstanceStatus",
    "InvocationStatus",
    "DeliverConsumptionStatus",
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
    # Declarative graph spec
    "GraphSpec",
    "NodeSpec",
    "EdgeSpec",
    # Runtime graph instance
    "GraphInstance",
    # Graph spec persistence
    "GraphSpecStore",
    "InMemoryGraphSpecStore",
    "SqliteGraphSpecStore",
    # Graph instance persistence
    "GraphInstanceStore",
    "InMemoryGraphInstanceStore",
    "SqliteGraphInstanceStore",
    # Node state persistence
    "NodeStateStore",
    "InMemoryNodeStateStore",
    "SqliteNodeStateStore",
    # Node state in-memory abstraction
    "NodeState",
    "SimpleNodeState",
    "NullNodeState",
    "SqliteNodeState",
    "NodeInvocationRecord",
    "NodeStateFactory",
    "NullNodeStateFactory",
    "SimpleNodeStateFactory",
    "SqliteNodeStateFactory",
    "GraphMetadata",
    "InvocationContext",
    "RecoveryContext",
    "GraphStateSnapshot",
    "GraphMetadataStore",
    "NullGraphMetadataStore",
    "MemoryGraphMetadataStore",
    "SqliteGraphMetadataStore",
    "GraphPersistenceCoordinator",
    "create_null_coordinator",
    "StateSchema",
    "StateFieldSpec",
    "NodeFactory",
    "NodeRegistry",
    "StateFactory",
    "StateRegistry",
    "SimpleStateFactory",
    "DynamicStateFactory",
    # Generic Node types + factories
    "FunctionNode",
    "FunctionNodeFactory",
    "GraphAsNode",
    "GraphAsNodeFactory",
    "DelayNode",
    "DelayNodeFactory",
    "HumanInputNode",
    "HumanInputNodeFactory",
    # Declarative → imperative bridge
    "GraphSpecCompiler",
    "TopologyValidator",
    "TopologyError",
]

__version__ = "1.0.0"
