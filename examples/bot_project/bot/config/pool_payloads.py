"""Pool tree payloads — frozen Pydantic models for the pool/MCP/skills/prompt API.

These are the wire/value objects that cross the HTTP API <-> store boundary for
Phase 2A. Every model is ``frozen=True, extra="forbid"`` (architecture r12 /
type-safety r10): unknown keys are rejected at the boundary and instances are
immutable.

Mapping decisions (documented here so the stores stay consistent):

* ``McpServerEntry`` is NOT defined here — the bot reuses the framework
  :class:`modex_agent.ioc.configs.mcp.MCPServerEntry` (transport normalizer,
  ``type`` <-> ``transport`` alias, ``environment`` -> ``env`` input alias,
  rule-12 frozen/``extra="forbid"``). Import it from there directly.
* ``MainAgentNode`` carries ONLY the Phase-1 main-agent editable fields
  (``AgentConfig`` minus memory/experience, which are baked and not in the
  payload). It is written into ``config/pools/<name>/pool.yml``'s ``agents:``
  block by :class:`bot.config.pool_store.PoolStore`.
* ``SubagentNode`` carries ONLY the Phase-1 subagent editable fields
  (``AgentTemplate`` minus ``system_prompt_mode`` / ``fork_max_messages`` /
  ``memory`` / ``approval`` / ``experience``, which are
  NOT in the editable payload). It is written one-per-file under
  ``config/pools/<name>/templates/<agent_name>.yml``.
* ``ApprovalConfig`` / ``ApprovalEntry`` mirror ``ioc/configs/approval.py`` but
  are frozen value objects for the API boundary (the framework config stays
  mutable at runtime).

Phase-1 spec reference: see STEP 0 in the Phase 2A task brief.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from modex_agent.tools.presets import (
    DEFAULT_FORK_MAX_MESSAGES,
    MAX_FORK_MAX_MESSAGES,
    ContextMode,
    SystemPromptMode,
    ToolPreset,
)

# ─── Approval ────────────────────────────────────────────────────────────────


class ApprovalEntry(BaseModel):
    """Per-tool approval rules (mirrors ``ioc.configs.approval.ToolApprovalEntry``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_paths: list[str] = Field(default_factory=list)


class ApprovalConfig(BaseModel):
    """Agent approval configuration (mirrors ``ioc.configs.approval.ApprovalConfig``).

    Default OFF; set ``enabled: true`` to opt in. Tools not listed in ``tools``
    are auto-allowed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    tools: dict[str, ApprovalEntry] = Field(default_factory=dict)


# ─── Pool tree ───────────────────────────────────────────────────────────────


class MainAgentNode(BaseModel):
    """The editable main-agent node of a pool tree.

    Memory is baked (``bot.config.memory_defaults.main_agent_memory``) and
    therefore NOT part of the payload. ``experience`` and ``skills`` are
    likewise excluded: experience is baked-on for main agents, and skills are
    disk-only symlinks under ``skills/<pool>/<agent>/`` (single source of
    truth = disk, managed by :class:`bot.config.skills_store.SkillsStore`).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    max_steps: int = 100
    use_terminal: bool = False
    terminal_visibility: bool = False
    tool_preset: ToolPreset = ToolPreset.FULL
    tool_supplements: list[str] = Field(default_factory=list)
    approval: ApprovalConfig | None = None
    mcp: list[str] = Field(default_factory=list)


class SubagentNode(BaseModel):
    """A subagent node of a pool tree.

    Only the Phase-1 editable ``AgentTemplate`` fields are present.
    ``memory`` / ``approval`` / ``experience`` / ``skills`` are not part of the
    payload: skills are disk-only symlinks (single source of truth = disk).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent_name: str
    description: str = ""
    max_steps: int = 80
    tool_preset: ToolPreset = ToolPreset.READ_WRITE
    tool_supplements: list[str] = Field(default_factory=list)
    context_mode: ContextMode = ContextMode.FRESH
    mcp: list[str] = Field(default_factory=list)
    system_prompt_mode: SystemPromptMode = SystemPromptMode.REPLACE
    fork_max_messages: int = Field(
        default=DEFAULT_FORK_MAX_MESSAGES, ge=1, le=MAX_FORK_MAX_MESSAGES
    )


class PoolTree(BaseModel):
    """The full editable tree of one pool: its main agent + subagents.

    ``restart_required`` is an API-side hint (set by callers when a structural
    change needs a pool restart); it is NOT persisted to disk by the store.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    main_agent_name: str
    main: MainAgentNode
    subagents: list[SubagentNode] = Field(default_factory=list)
    restart_required: bool = False


# ─── Prompt ──────────────────────────────────────────────────────────────────


class PromptContent(BaseModel):
    """The content of an agent prompt markdown file (``agents/<name>.md``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    content: str


# ─── Skills ──────────────────────────────────────────────────────────────────


class SkillEntry(BaseModel):
    """A skill name, source, and short description parsed from SKILL.md."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    source: Literal["global", "local"] = "global"
    description: str = ""


# ─── Pool listing ────────────────────────────────────────────────────────────


class PoolSummary(BaseModel):
    """A one-line summary of a pool for the listing endpoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    main_agent_name: str
    subagent_count: int
