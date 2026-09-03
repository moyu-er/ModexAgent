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

from importlib import import_module
from typing import Any

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

_SYMBOL_MODULE = {
    **dict.fromkeys(("AgentType", "ComponentFactory", "ComponentSlot", "HookFactory", "HookRunnerKind", "MemoryHookFactory", "PluginSource", "PrototypeFactory", "ReactHookFactory", "SimpleFactory"), "modex_agent.plugins.abc"),
    **dict.fromkeys(("AssembledAgent", "AssemblyBuilder"), "modex_agent.plugins.assembly.builder"),
    **dict.fromkeys(("AgentContext", "AssemblyContext", "PoolContext", "PoolRuntimeDeps", "WorkspaceContext", "agent_context_chain"), "modex_agent.plugins.assembly.context"),
    "LlmDefaults": "modex_agent.plugins.assembly.native_core",
    **dict.fromkeys(("AssemblyPipeline", "AssemblyStage"), "modex_agent.plugins.assembly.pipeline"),
    **dict.fromkeys(("AssemblySpec", "MemoryOverrides"), "modex_agent.plugins.assembly.spec"),
    **dict.fromkeys(("AgentDeclarationView", "AgentDeclaredFields", "Capability", "CapabilityBinding", "CapabilityConfig", "CapabilityContribution", "CapabilitySupply", "CapabilityWiring", "ChildSummary", "FinalRosterView", "PoolSupplyAgentEntry", "PoolSupplyView", "PromptSectionSpec", "ToolReplacementSpec", "TreePositionView"), "modex_agent.plugins.capability"),
    **dict.fromkeys(("ComponentRegistryLoader", "Plugin", "PluginDiscoveryConfig", "PluginRegistrationContext"), "modex_agent.plugins.loader"),
    **dict.fromkeys(("ComponentNotFoundError", "ComponentRegistry", "TypedBundle"), "modex_agent.plugins.registry"),
}


def __getattr__(name: str) -> Any:
    module_name = _SYMBOL_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
