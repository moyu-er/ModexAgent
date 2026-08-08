"""Generic `Node` implementations + their `NodeFactory` classes.

Framework-provided reusable node types that use the deliver/submit model
from the deliver/submit design. Each module pairs a `Node` subclass with a matching
`NodeFactory` for declarative construction via `NodeRegistry`.

Modules:

- `function_node` — `FunctionNode` wraps a sync/async function.
- `graph_as_node` — `GraphAsNode` wraps a `CompiledGraph` (ADR-0033 D8).
- `delay_node` — `DelayNode` for async delay / rate-limiting.
- `human_input_node` — `HumanInputNode` suspends via `GraphInterrupt`.

Each factory declares a Pydantic `config_schema()` model — the
`NodeRegistry` validates `NodeSpec.config` against it before `create()` is
called, and `create()` re-validates to obtain a typed config object.
Runtime validation (function registry lookup, GraphSpec compilation) stays
in `create()`. Register them with `NodeRegistry.register(...)` at startup;
`GraphSpecCompiler` resolves them at compile time.
"""

from __future__ import annotations

from .delay_node import DelayNode, DelayNodeConfig, DelayNodeFactory
from .end_node import DefaultEndNodeFactory, EndNode
from .function_node import FunctionNode, FunctionNodeConfig, FunctionNodeFactory
from .graph_as_node import GraphAsNode, GraphAsNodeConfig, GraphAsNodeFactory
from .human_input_node import HumanInputNode, HumanInputNodeConfig, HumanInputNodeFactory
from .start_node import DefaultStartNodeFactory, StartNode

__all__ = [
    "DelayNode",
    "DelayNodeConfig",
    "DelayNodeFactory",
    "DefaultEndNodeFactory",
    "DefaultStartNodeFactory",
    "EndNode",
    "FunctionNode",
    "FunctionNodeConfig",
    "FunctionNodeFactory",
    "GraphAsNode",
    "GraphAsNodeConfig",
    "GraphAsNodeFactory",
    "HumanInputNode",
    "HumanInputNodeConfig",
    "HumanInputNodeFactory",
    "StartNode",
]
