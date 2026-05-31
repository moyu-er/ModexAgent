# framework/multi_agent/template.py
"""AgentTemplate — preset definition for dynamically created subagents."""

from __future__ import annotations

from dataclasses import dataclass

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig


@dataclass
class AgentTemplate:
    """Preset definition for a dynamically creatable subagent type.

    Communication tools (send_to_agent + list_communication_targets) are
    auto-injected by the framework — they must not appear in template config.
    """
    agent_type: str
    description: str = ""
    max_steps: int = 20
    standard_tools: bool = True
    use_terminal: bool = True
    terminal_visibility: str = "visible"  # "visible" | "hidden" — initial preference
    memory: MemoryConfig | None = None
    skills: SkillsConfig | None = None
