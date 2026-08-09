"""modex_graph — generalized typed graph engine.

Framework-agnostic sibling of `modex_agent`. Depends only on
`pydantic` + the Python standard library. The reverse dependency
(`modex_agent` → `modex_graph`) is required; `modex_graph` → `modex_agent`
is FORBIDDEN and enforced by `tests/architecture/test_modex_graph_isolation.py`.

Public surface (ADR-0033 acceptance criteria):

- **Graph primitives:** `Graph`, `Node`, `CompiledGraph`, `GraphEngine`,
  `GraphNode` (START/END sentinels).
- **Context + runtime:** `GraphContext`, `GraphRuntime`, `GraphRunControl`.
- **State:** `GraphState`.
- **Deliver/submit:** `IntegratedPayload`, `IntegratedInput`,
  `InputIntegrator`, `DefaultInputIntegrator`, `DeliverStore`,
  `InMemoryDeliverStore`, `SqliteDeliverStore`, `DeliverRecord`.
- **Scheduler:** `Scheduler` (ABC), `LinearScheduler`, `ParallelScheduler`,
  `SchedulerKind`, `NodeInstanceStatus`, `NodeInstance`, `NodeTrigger`.
- **Lifecycle + interrupt:** `GraphInstanceStatus`,
  `InterruptPolicy` (ABC), `CrashPolicy` (default).
- **Exceptions:** `GraphBubbleUp`, `GraphInterrupt`, `GraphDrained`,
  `ParentCommand`, `RoutingError`, `GraphRecursionError`.

See `docs/adr/0033-generalized-graph-engine.md` for the authoritative design.
"""

from __future__ import annotations

from .compiled_graph import CompiledGraph
from .constants import (
    DeliverConsumptionStatus,
    GraphInstanceStatus,
    GraphNode,
    InvocationStatus,
    NodeInstanceStatus,
    NodeTrigger,
    SchedulerKind,
)
from .context import GraphContext
from .engine import GraphEngine
from .exceptions import (
    GraphBubbleUp,
    GraphDrained,
    GraphInterrupt,
    GraphRecursionError,
    InvocationStateError,
    ParentCommand,
    RoutingError,
)
from .graph import Edge, Graph
from .id_generator import IdGenerator, SnowflakeIdGenerator, default_id_generator
from .integration import (
    DefaultInputIntegrator,
    GraphPayload,
    InputIntegrator,
    IntegratedInput,
    IntegratedPayload,
)
from .interrupt_policy import CrashPolicy, InterruptPolicy
from .node import Node
from .node_factory import NodeFactory, NodeRegistry
from .nodes import (
    DefaultEndNodeFactory,
    DefaultStartNodeFactory,
    DelayNode,
    DelayNodeFactory,
    EndNode,
    FunctionNode,
    FunctionNodeFactory,
    GraphAsNode,
    GraphAsNodeFactory,
    HumanInputNode,
    HumanInputNodeFactory,
    StartNode,
)
from .output_adapter import GraphOutput, GraphOutputAdapter, GraphOutputKind
from .persistence import (
    CoordinatorFactory,
    DeliverRecord,
    DeliverStore,
    DeliverStoreFactory,
    GraphIORecord,
    GraphIORecordStore,
    GraphInstance,
    GraphInstanceStore,
    GraphMetadata,
    GraphPersistenceCoordinator,
    GraphStateSnapshot,
    InMemoryDeliverStore,
    InMemoryDeliverStoreFactory,
    InMemoryGraphIORecordStore,
    InMemoryGraphInstanceStore,
    InMemoryNodeStateStore,
    InvocationContext,
    NodeInvocationRecord,
    NodeStateStore,
    NullCoordinatorFactory,
    NullDeliverStore,
    NullDeliverStoreFactory,
    NullGraphIORecordStore,
    NullGraphInstanceStore,
    NullNodeStateStore,
    SqliteDeliverStore,
    SqliteDeliverStoreFactory,
    SqliteGraphIORecordStore,
    SqliteGraphInstanceStore,
    SqliteNodeStateStore,
    create_null_coordinator,
)
from .run_control import GraphRunControl
from .runtime import GraphRuntime
from .scheduler import LinearScheduler, NodeInstance, ParallelScheduler, Scheduler
from .spec import EdgeSpec, GraphSpec, NodeSpec
from .spec_compiler import GraphSpecCompiler
from .spec_store import (
    GraphSpecStore,
    InMemoryGraphSpecStore,
    SqliteGraphSpecStore,
)
from .state import DefaultGraphState, GraphState
from .topology_validator import TopologyError, TopologyValidator
from .utils import generate_id

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
    "GraphRunControl",
    "GraphOutput",
    "GraphOutputAdapter",
    "GraphOutputKind",
    # State
    "GraphState",
    "DefaultGraphState",
    # ID generation
    "IdGenerator",
    "SnowflakeIdGenerator",
    "default_id_generator",
    "generate_id",
    # Deliver/submit persistence
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
    "GraphPayload",
    # Scheduler
    "Scheduler",
    "LinearScheduler",
    "ParallelScheduler",
    "SchedulerKind",
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
    "RoutingError",
    "GraphRecursionError",
    "InvocationStateError",
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
    "NullGraphInstanceStore",
    "InMemoryGraphInstanceStore",
    "SqliteGraphInstanceStore",
    # Graph I/O record persistence
    "GraphIORecord",
    "GraphIORecordStore",
    "NullGraphIORecordStore",
    "InMemoryGraphIORecordStore",
    "SqliteGraphIORecordStore",
    # Node state persistence (lifecycle + CAS + version chain)
    "NodeStateStore",
    "NullNodeStateStore",
    "InMemoryNodeStateStore",
    "SqliteNodeStateStore",
    "NodeInvocationRecord",
    "GraphMetadata",
    "InvocationContext",
    "GraphStateSnapshot",
    "GraphPersistenceCoordinator",
    "CoordinatorFactory",
    "NullCoordinatorFactory",
    "create_null_coordinator",
    "NodeFactory",
    "NodeRegistry",
    # Generic Node types + factories
    "FunctionNode",
    "FunctionNodeFactory",
    "GraphAsNode",
    "GraphAsNodeFactory",
    "DelayNode",
    "DelayNodeFactory",
    "DefaultEndNodeFactory",
    "DefaultStartNodeFactory",
    "EndNode",
    "HumanInputNode",
    "HumanInputNodeFactory",
    "StartNode",
    # Declarative → imperative bridge
    "GraphSpecCompiler",
    "TopologyValidator",
    "TopologyError",
]

__version__ = "1.0.0"
