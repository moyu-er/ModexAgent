# framework/multi_agent/template_registry.py
"""AgentTemplateRegistry — scans and loads per-pool subagent templates."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.ioc.configs.skills import SkillsConfig
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.tools.presets import ContextMode, SystemPromptMode, ThinkingBudget, ToolPreset

logger = logging.getLogger(__name__)


class AgentTemplateRegistry:
    """Scans config/pools/*/templates/*.yml and loads AgentTemplate definitions.

    Templates are isolated by pool_name — a template only exists within
    the pool directory it's defined in.
    """

    def __init__(self, project_dir: Path) -> None:
        self._templates: dict[str, dict[str, AgentTemplate]] = {}
        self._load(project_dir)

    def _load(self, project_dir: Path) -> None:
        pools_dir = project_dir / "config" / "pools"
        if not pools_dir.exists():
            return

        for pool_dir in pools_dir.iterdir():
            if not pool_dir.is_dir():
                continue
            templates_dir = pool_dir / "templates"
            if not templates_dir.exists():
                continue

            pool_name = pool_dir.name
            self._templates[pool_name] = {}

            for yml_path in templates_dir.glob("*.yml"):
                try:
                    with open(yml_path, encoding="utf-8") as f:
                        raw = yaml.safe_load(f)
                    if not raw or "agent_type" not in raw:
                        logger.warning("Skipping invalid template: %s", yml_path)
                        continue

                    # Parse tool_preset — defaults to READ_WRITE if not specified
                    tool_preset_raw = raw.get("tool_preset")
                    if tool_preset_raw is not None:
                        try:
                            tool_preset = ToolPreset(tool_preset_raw)
                        except ValueError:
                            logger.warning(
                                "Invalid tool_preset '%s' in %s, falling back to 'read_write'",
                                tool_preset_raw,
                                yml_path,
                            )
                            tool_preset = ToolPreset.READ_WRITE
                    else:
                        tool_preset = ToolPreset.READ_WRITE

                    context_mode_raw = raw.get("context_mode", "fresh")
                    try:
                        context_mode = ContextMode(context_mode_raw)
                    except ValueError:
                        logger.warning(
                            "Invalid context_mode '%s' in %s, falling back to 'fresh'",
                            context_mode_raw,
                            yml_path,
                        )
                        context_mode = ContextMode.FRESH

                    thinking_budget_raw = raw.get("thinking_budget", "medium")
                    try:
                        thinking_budget = ThinkingBudget(thinking_budget_raw)
                    except ValueError:
                        logger.warning(
                            "Invalid thinking_budget '%s' in %s, falling back to 'medium'",
                            thinking_budget_raw,
                            yml_path,
                        )
                        thinking_budget = ThinkingBudget.MEDIUM

                    system_prompt_mode_raw = raw.get("system_prompt_mode", "replace")
                    try:
                        system_prompt_mode = SystemPromptMode(system_prompt_mode_raw)
                    except ValueError:
                        logger.warning(
                            "Invalid system_prompt_mode '%s' in %s, falling back to 'replace'",
                            system_prompt_mode_raw,
                            yml_path,
                        )
                        system_prompt_mode = SystemPromptMode.REPLACE

                    fork_max_messages = raw.get("fork_max_messages", 80)
                    if (
                        isinstance(fork_max_messages, bool)
                        or not isinstance(fork_max_messages, int)
                        or fork_max_messages < 1
                    ):
                        fork_max_messages = 80

                    template = AgentTemplate(
                        agent_type=raw["agent_type"],
                        description=raw.get("description", ""),
                        max_steps=raw.get("max_steps", 20),
                        tool_preset=tool_preset,
                        use_terminal=raw.get("use_terminal", True),
                        terminal_visibility=raw.get("terminal_visibility", True),
                        context_mode=context_mode,
                        thinking_budget=thinking_budget,
                        default_reads=raw.get("default_reads", []),
                        visible_targets=raw.get("visible_targets"),
                        system_prompt_mode=system_prompt_mode,
                        fork_max_messages=fork_max_messages,
                        memory=(
                            MemoryConfig.model_validate(raw["memory"])
                            if raw.get("memory")
                            else None
                        ),
                        skills=(
                            SkillsConfig(roots=raw["skills"]["roots"])
                            if raw.get("skills")
                            else None
                        ),
                    )
                    self._templates[pool_name][template.agent_type] = template
                    logger.debug("Loaded template %s for pool %s", template.agent_type, pool_name)
                except Exception:
                    logger.exception("Failed to load template: %s", yml_path)

    def list_templates(self, pool_name: str) -> list[AgentTemplate]:
        return list(self._templates.get(pool_name, {}).values())

    def get_template(self, pool_name: str, agent_type: str) -> AgentTemplate | None:
        return self._templates.get(pool_name, {}).get(agent_type)
