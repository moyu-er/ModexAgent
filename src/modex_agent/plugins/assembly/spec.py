"""AssemblySpec + MemoryOverrides — frozen Pydantic input to the assembly
pipeline (SPEC §6.6).

AssemblySpec carries component-name references (strings) + per-component
config dicts + a WorkspaceContext reference. No component instances are
held — only names. This makes the spec serializable, auditable, and
decoupled from the ComponentRegistry (late binding at assembly time).

MemoryOverrides is the per-agent memory config override surface (SPEC §5.5).
All fields optional — None means "no override, use framework default
MemoryConfig" (see ``modex_agent.memory.presets``).

Design constraints:
- Rule 12: frozen=True, extra="forbid" for config value objects.
- arbitrary_types_allowed on AssemblySpec for WorkspaceContext (a frozen
  dataclass, not a Pydantic BaseModel — rule 11 leaf value-object).
- Rule 14: dict[str, Any] for open extension payloads (per-component
  configs are open — keys vary per component).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.plugins.abc import AgentType
from modex_agent.workspace.context import WorkspaceContext


class MemoryOverrides(BaseModel):
    """Per-agent memory config overrides (SPEC §5.5).

    All fields optional — None means "no override, use framework default
    MemoryConfig". A non-None value overrides the corresponding setting in
    the default MemoryConfig (``modex_agent.memory.presets``).

    Governance is v1-derived from MemoryConfig (not configurable here);
    the v2 GOVERNANCE slot will add a governance field. Structure-ready:
    adding fields later is non-breaking because all current fields default
    to None.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_context_tokens: int | None = None
    """Session layer max context token override (compression threshold)."""

    archive_enabled: bool | None = None
    """Archive layer toggle. None = inherit default (off for subagent, toggleable for main)."""

    core_enabled: bool | None = None
    """Core memory layer toggle. None = inherit default."""


class AssemblySpec(BaseModel):
    """Frozen Pydantic input to the assembly pipeline (SPEC §6.6).

    Carries component-name references (strings) + per-component config dicts
    + a WorkspaceContext reference. No component instances — only names.

    ``agent_type`` is derived from ``provider_kind`` × main/sub by the
    scope compiler. ``workspace_ctx`` is a Python object reference
    (arbitrary_types_allowed — WorkspaceContext is a frozen dataclass, not
    a Pydantic model).

    Fields per SPEC §6.6:
    - agent_type: native_main / native_sub / external_main / external_sub
    - agent_name + pool_name: identity
    - 4 per-agent slots: tools/hooks/llm_provider/system_prompt_provider
      (+ configs)
    - memory_overrides: MemoryOverrides merged with default MemoryConfig
    - execution_strategy + provider_kind: pool-level
    - workspace_ctx: Python object reference (not serialized)
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    agent_type: AgentType
    agent_name: str
    pool_name: str
    description: str = ""
    max_iterations: int = 15
    roles: list[str] = Field(default_factory=list)

    # ── per-agent slots: component names + per-component config ──
    tools: list[str]
    tool_configs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    hooks: list[str]
    hook_configs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    llm_provider: str
    llm_provider_config: dict[str, Any] = Field(default_factory=dict)
    system_prompt_provider: str
    system_prompt_config: dict[str, Any]
    memory_overrides: MemoryOverrides
    memory_system: str | None = None
    # Open extension payload: single memory-system factory config (rule 12 —
    # matches llm_provider_config pattern; typed by ComponentFactory.config_model).
    memory_system_config: dict[str, Any] = Field(default_factory=dict)

    # ── pool-level slots (from the declared pool root) ──
    execution_strategy: str
    provider_kind: str | None = None
    mcp_servers: list[str] = Field(default_factory=list)
    interceptors: list[str] = Field(default_factory=list)
    interceptor_configs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    commands: list[str] | None = None

    # ── context reference (Python object, not serialized) ──
    workspace_ctx: WorkspaceContext
