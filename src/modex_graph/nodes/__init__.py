"""Generic `Node` implementations + their `NodeFactory` classes (ticket 02).

Framework-provided reusable node types that use the deliver/submit model
from ticket 07 (P1A). Each module pairs a `Node` subclass with a matching
`NodeFactory` for declarative construction via `NodeRegistry`.

Modules:

- `function_node` — `FunctionNode` wraps a sync/async function.
- `graph_as_node` — `GraphAsNode` wraps a `CompiledGraph` (ADR-0033 D8).
- `delay_node` — `DelayNode` for async delay / rate-limiting.
- `human_input_node` — `HumanInputNode` suspends via `GraphInterrupt`.

All factories return `None` from `config_schema()` — validation is done in
`create()` against the factory's own state (function registry, compiler,
or simple type coercion). Register them with `NodeRegistry.register(...)`
at startup; `GraphSpecCompiler` resolves them at compile time.
"""

from __future__ import annotations

from .delay_node import DelayNode, DelayNodeFactory
from .function_node import FunctionNode, FunctionNodeFactory
from .graph_as_node import GraphAsNode, GraphAsNodeFactory
from .human_input_node import HumanInputNode, HumanInputNodeFactory

__all__ = [
    "DelayNode",
    "DelayNodeFactory",
    "FunctionNode",
    "FunctionNodeFactory",
    "GraphAsNode",
    "GraphAsNodeFactory",
    "HumanInputNode",
    "HumanInputNodeFactory",
]
