"""Plugin-unified agent assembly system — public API.

Converged package exports for the component-factory-based plugin
system (SPEC §4-§6). Submodules:

- ``abc`` — ``ComponentSlot``, ``AgentType``, ``HookRunnerKind``,
  ``PluginSource`` and the factory hierarchy (``ComponentFactory``,
  ``SimpleFactory`` (singleton semantics), ``PrototypeFactory``
  (per-assembly construction), ``HookFactory``, ``ReactHookFactory``,
  ``MemoryHookFactory``).
- ``capability`` — the capability-bundle protocol (ADR-0047):
  ``Capability``, ``CapabilitySupply``, ``CapabilityConfig`` and the
  five-phase payload types (declaration/tree/roster views,
  contributions, bindings, supply views, wiring).
- ``registry`` — ``ComponentRegistry``, ``ComponentNotFoundError``,
  ``TypedBundle``.
- ``loader`` — ``Plugin``, ``PluginRegistrationContext``,
  ``PluginDiscoveryConfig``, ``ComponentRegistryLoader``.
- ``assembly.context`` — ``AssemblyContext``, ``PoolRuntimeDeps``,
  the context-chain carriers (``WorkspaceContext``/``PoolContext``/
  ``AgentContext``).
- ``assembly.builder`` — ``AssembledAgent``, ``AssemblyBuilder``.
- ``assembly.pipeline`` — ``AssemblyPipeline``, ``AssemblyStage``.
- ``assembly.spec`` — ``AssemblySpec``, ``MemoryOverrides``.
"""

from __future__ import annotations

from modex_agent.plugins.abc import (
    AgentType,
    ComponentFactory,
    ComponentSlot,
    HookFactory,
    HookRunnerKind,
    MemoryHookFactory,
    PluginSource,
    PrototypeFactory,
    ReactHookFactory,
    SimpleFactory,
)
from modex_agent.plugins.assembly.builder import AssembledAgent, AssemblyBuilder
from modex_agent.plugins.assembly.context import (
    AgentContext,
    AssemblyContext,
    PoolContext,
    PoolRuntimeDeps,
    WorkspaceContext,
    agent_context_chain,
)
from modex_agent.plugins.assembly.native_core import LlmDefaults
from modex_agent.plugins.assembly.pipeline import AssemblyPipeline, AssemblyStage
from modex_agent.plugins.assembly.spec import AssemblySpec, MemoryOverrides
from modex_agent.plugins.capability import (
    AgentDeclarationView,
    AgentDeclaredFields,
    Capability,
    CapabilityBinding,
    CapabilityConfig,
    CapabilityContribution,
    CapabilitySupply,
    CapabilityWiring,
    ChildSummary,
    FinalRosterView,
    PoolSupplyAgentEntry,
    PoolSupplyView,
    PromptSectionSpec,
    ToolReplacementSpec,
    TreePositionView,
)
from modex_agent.plugins.loader import (
    ComponentRegistryLoader,
    Plugin,
    PluginDiscoveryConfig,
    PluginRegistrationContext,
)
from modex_agent.plugins.registry import (
    ComponentNotFoundError,
    ComponentRegistry,
    TypedBundle,
)

__all__ = [
    "AgentContext",
    "AgentDeclaredFields",
    "AgentDeclarationView",
    "AgentType",
    "AssembledAgent",
    "AssemblyBuilder",
    "AssemblyContext",
    "AssemblyPipeline",
    "AssemblySpec",
    "AssemblyStage",
    "Capability",
    "CapabilityBinding",
    "CapabilityConfig",
    "CapabilityContribution",
    "CapabilitySupply",
    "CapabilityWiring",
    "ChildSummary",
    "ComponentFactory",
    "ComponentNotFoundError",
    "ComponentRegistry",
    "ComponentRegistryLoader",
    "ComponentSlot",
    "FinalRosterView",
    "HookFactory",
    "HookRunnerKind",
    "LlmDefaults",
    "MemoryHookFactory",
    "MemoryOverrides",
    "Plugin",
    "PluginDiscoveryConfig",
    "PluginRegistrationContext",
    "PluginSource",
    "PoolContext",
    "PoolRuntimeDeps",
    "PoolSupplyAgentEntry",
    "PoolSupplyView",
    "PromptSectionSpec",
    "PrototypeFactory",
    "ReactHookFactory",
    "SimpleFactory",
    "ToolReplacementSpec",
    "TreePositionView",
    "TypedBundle",
    "WorkspaceContext",
    "agent_context_chain",
]
