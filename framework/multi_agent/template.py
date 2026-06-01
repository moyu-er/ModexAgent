# framework/multi_agent/template.py
"""AgentTemplate — preset definition for dynamically created subagents."""

from __future__ import annotations

from dataclasses import dataclass, field

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.tools.presets import ToolPreset


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (send_to_agent + list_communication_targets) are
    auto-injected by the framework — they must not appear in template config.

    Pi-aligned fields (tool_preset, context_mode, thinking_budget,
    default_reads, progress_tracking) were added for the coding pool
    redesign. They have no runtime effect unless the communication
    service chooses to act on them.
    """

    agent_type: str
    description: str = ""

    # ── lifecycle ──
    max_steps: int = 20

    # ── tool policy (backward-compatible) ──
    # When tool_preset is present, it takes precedence over standard_tools.
    standard_tools: bool = True
    tool_preset: ToolPreset = ToolPreset.FULL
    use_terminal: bool = True
    terminal_visibility: bool = True  # True=prefer visible, False=prefer hidden

    # ── pi-aligned fields ──
    context_mode: str = "fresh"          # "fresh" | "fork"
    thinking_budget: str = "medium"      # "low" | "medium" | "high" — prompt annotation only
    default_reads: list[str] = field(default_factory=list)
    progress_tracking: bool = False
    visible_targets: list[str] | None = None  # None=all NORMAL agents visible; list=restrict

    # ── optional subsystems ──
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
