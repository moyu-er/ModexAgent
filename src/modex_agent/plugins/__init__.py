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
    **{
        name: "modex_agent.plugins.abc"
        for name in (
            "AgentType",
            "ComponentFactory",
            "ComponentSlot",
            "HookFactory",
            "HookRunnerKind",
            "MemoryHookFactory",
            "PluginSource",
            "PrototypeFactory",
            "ReactHookFactory",
            "SimpleFactory",
        )
    },
    **{
        name: "modex_agent.plugins.assembly.builder"
        for name in ("AssembledAgent", "AssemblyBuilder")
    },
    **{
        name: "modex_agent.plugins.assembly.context"
        for name in (
            "AgentContext",
            "AssemblyContext",
            "PoolContext",
            "PoolRuntimeDeps",
            "WorkspaceContext",
            "agent_context_chain",
        )
    },
    "LlmDefaults": "modex_agent.plugins.assembly.native_core",
    **{
        name: "modex_agent.plugins.assembly.pipeline"
        for name in ("AssemblyPipeline", "AssemblyStage")
    },
    **{
        name: "modex_agent.plugins.assembly.spec"
        for name in ("AssemblySpec", "MemoryOverrides")
    },
    **{
        name: "modex_agent.plugins.capability"
        for name in (
            "AgentDeclarationView",
            "AgentDeclaredFields",
            "Capability",
            "CapabilityBinding",
            "CapabilityConfig",
            "CapabilityContribution",
            "CapabilitySupply",
            "CapabilityWiring",
            "ChildSummary",
            "FinalRosterView",
            "PoolSupplyAgentEntry",
            "PoolSupplyView",
            "PromptSectionSpec",
            "ToolReplacementSpec",
            "TreePositionView",
        )
    },
    **{
        name: "modex_agent.plugins.loader"
        for name in (
            "ComponentRegistryLoader",
            "Plugin",
            "PluginDiscoveryConfig",
            "PluginRegistrationContext",
        )
    },
    **{
        name: "modex_agent.plugins.registry"
        for name in ("ComponentNotFoundError", "ComponentRegistry", "TypedBundle")
    },
}


def __getattr__(name: str) -> Any:
    module_name = _SYMBOL_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
