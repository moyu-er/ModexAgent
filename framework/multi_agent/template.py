# framework/multi_agent/template.py
"""AgentTemplate — preset definition for dynamically created subagents."""

from __future__ import annotations

from dataclasses import dataclass, field

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.tools.presets import ContextMode, SystemPromptMode, ThinkingBudget, ToolPreset


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (send_to_agent) are auto-injected by the framework —
    they must not appear in template config.

    Pi-aligned fields (tool_preset, context_mode, thinking_budget,
    default_reads, progress_tracking) were added for the coding pool
    redesign. tool_preset controls tool registration; context_mode
    controls memory inheritance.

    thinking_budget is reserved for future LLM reasoning-budget control
    — it is parsed from YAML but not yet consumed by any LLM config path.

    default_reads is reserved for future use — parsed from YAML but
    not yet injected into subagent context by the framework.
    """

    agent_type: str
    description: str = ""

    # ── lifecycle ──
    max_steps: int = 20

    # ── tool policy ──
    # standard_tools is DEPRECATED; use tool_preset instead.
    # When template YAML has standard_tools: false without tool_preset,
    # template_registry translates it to tool_preset=NONE.
    standard_tools: bool = True
    tool_preset: ToolPreset = ToolPreset.FULL
    use_terminal: bool = True
    terminal_visibility: bool = True  # True=prefer visible, False=prefer hidden

    # ── pi-aligned fields ──
    context_mode: ContextMode = ContextMode.FRESH
    thinking_budget: ThinkingBudget = ThinkingBudget.MEDIUM
    default_reads: list[str] = field(default_factory=list)
    progress_tracking: bool = False
    visible_targets: list[str] | None = None  # None=all NORMAL agents visible; list=restrict

    # ── system prompt control ──
    system_prompt_mode: SystemPromptMode = SystemPromptMode.REPLACE

    # ── fork context control ──
    fork_max_messages: int = 80  # only meaningful when context_mode == FORK

    # ── optional subsystems ──
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
