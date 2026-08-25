"""Scope declaration package — declaration types, loading, position-derived
defaults, two-phase tree validation, profiles, and the pure-function
ScopeCompiler (ADR-0042, SPEC §3 + §7). Pure types, parsing, validation,
and compilation: zero consumer wiring until ticket 07 boots the first pool
from a declaration.
"""

from modex_agent.scope.compiler import (
    AgentProvenance,
    CompiledAgent,
    FieldProvenance,
    ProvenanceLayer,
    ScopeCompilation,
    ToolEntryProvenance,
    ToolOrigin,
    ToolReplacement,
    compile_scope,
)
from modex_agent.scope.defaults import (
    MemoryPreset,
    PositionDefaults,
    RegistrationTiming,
    defaults_for_position,
    effective_defaults,
    memory_config_for_position,
)
from modex_agent.scope.loader import (
    ScopeDeclarationError,
    load_dynamic_workspace_declarations,
    load_scope_declaration,
)
from modex_agent.scope.overlay import (
    AgentOverlay,
    PoolOverlay,
    ScopeOverlay,
    apply_scope_overlay,
)
from modex_agent.scope.profile import (
    STANDARD_PROFILES,
    Profile,
    ProfileStore,
    merge_memory_declarations,
)
from modex_agent.scope.seam import ScopeGenerationTracker, spec_hash
from modex_agent.scope.spec import (
    AgentSpec,
    MemoryDeclaration,
    PoolSpec,
    ScopeKind,
    ScopeSpec,
    SessionMemoryOverride,
    WorkspacePathsSpec,
    WorkspacePersistenceSpec,
    WorkspaceSpec,
)
from modex_agent.scope.validator import (
    EffectiveAgentConfig,
    GraphAgentReference,
    ProfileDeclaration,
    RuleId,
    ScopeValidationIssue,
    validate_declaration,
    validate_effective_configs,
)

__all__ = [
    "AgentProvenance",
    "AgentOverlay",
    "AgentSpec",
    "CompiledAgent",
    "EffectiveAgentConfig",
    "FieldProvenance",
    "GraphAgentReference",
    "MemoryDeclaration",
    "MemoryPreset",
    "PoolSpec",
    "PoolOverlay",
    "PositionDefaults",
    "Profile",
    "ProfileDeclaration",
    "ProfileStore",
    "ProvenanceLayer",
    "RegistrationTiming",
    "RuleId",
    "STANDARD_PROFILES",
    "ScopeCompilation",
    "ScopeDeclarationError",
    "ScopeGenerationTracker",
    "ScopeKind",
    "ScopeOverlay",
    "ScopeSpec",
    "ScopeValidationIssue",
    "SessionMemoryOverride",
    "ToolEntryProvenance",
    "ToolOrigin",
    "ToolReplacement",
    "WorkspacePathsSpec",
    "WorkspacePersistenceSpec",
    "WorkspaceSpec",
    "apply_scope_overlay",
    "compile_scope",
    "defaults_for_position",
    "effective_defaults",
    "load_dynamic_workspace_declarations",
    "load_scope_declaration",
    "memory_config_for_position",
    "merge_memory_declarations",
    "spec_hash",
    "validate_declaration",
    "validate_effective_configs",
]
