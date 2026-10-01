"""Plugin-unified agent assembly system — public API.

Runtime machinery of the component-factory plugin system (SPEC §4-§6).
The compile-time schema types (``ComponentSlot``, the factory ABCs,
``Capability`` payloads, ``AssemblySpec``, ``ComponentRegistry``) live in
``modex_agent.scope`` (W3a) and are imported downward from here.
Submodules:

- ``loader`` — ``Plugin``, ``PluginRegistrationContext``,
  ``PluginDiscoveryConfig``, ``ComponentRegistryLoader``.
- ``assembly.context`` — ``AssemblyContext``, ``PoolRuntimeDeps``,
  the context-chain carriers (``WorkspaceContext``/``PoolContext``/
  ``AgentContext``).
- ``assembly.builder`` — ``AssembledAgent``, ``AssemblyBuilder``.
- ``assembly.pipeline`` — ``AssemblyPipeline``, ``AssemblyStage``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

__all__ = [
    "AgentContext",
    "AssembledAgent",
    "AssemblyBuilder",
    "AssemblyContext",
    "AssemblyPipeline",
    "AssemblyStage",
    "ComponentRegistryLoader",
    "LlmDefaults",
    "Plugin",
    "PluginDiscoveryConfig",
    "PluginRegistrationContext",
    "PoolContext",
    "PoolRuntimeDeps",
    "WorkspaceContext",
    "agent_context_chain",
]

_SYMBOL_MODULE = {
    **dict.fromkeys(("AssembledAgent", "AssemblyBuilder"), "modex_agent.plugins.assembly.builder"),
    **dict.fromkeys(("AgentContext", "AssemblyContext", "PoolContext", "PoolRuntimeDeps", "WorkspaceContext", "agent_context_chain"), "modex_agent.plugins.assembly.context"),
    "LlmDefaults": "modex_agent.plugins.assembly.native_core",
    **dict.fromkeys(("AssemblyPipeline", "AssemblyStage"), "modex_agent.plugins.assembly.pipeline"),
    **dict.fromkeys(("ComponentRegistryLoader", "Plugin", "PluginDiscoveryConfig", "PluginRegistrationContext"), "modex_agent.plugins.loader"),
}


def __getattr__(name: str) -> Any:
    module_name = _SYMBOL_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
