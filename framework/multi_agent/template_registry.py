# framework/multi_agent/template_registry.py
"""AgentTemplateRegistry — scans and loads per-pool subagent templates."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from framework.ioc.configs.memory import MemoryConfig
from framework.ioc.configs.skills import SkillsConfig
from framework.multi_agent.template import AgentTemplate
from framework.tools.presets import ToolPreset

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

                    # Parse pi-aligned fields with fallback
                    tool_preset_raw = raw.get("tool_preset")
                    tool_preset = ToolPreset.FULL
                    if tool_preset_raw is not None:
                        try:
                            tool_preset = ToolPreset(tool_preset_raw)
                        except ValueError:
                            logger.warning(
                                "Invalid tool_preset '%s' in %s, falling back to 'full'",
                                tool_preset_raw, yml_path,
                            )

                    template = AgentTemplate(
                        agent_type=raw["agent_type"],
                        description=raw.get("description", ""),
                        max_steps=raw.get("max_steps", 20),
                        standard_tools=raw.get("standard_tools", True),
                        tool_preset=tool_preset,
                        use_terminal=raw.get("use_terminal", True),
                        terminal_visibility=raw.get("terminal_visibility", True),
                        context_mode=raw.get("context_mode", "fresh"),
                        thinking_budget=raw.get("thinking_budget", "medium"),
                        default_reads=raw.get("default_reads", []),
                        progress_tracking=raw.get("progress_tracking", False),
                        visible_targets=raw.get("visible_targets"),
                        memory=(
                            MemoryConfig.model_validate(raw["memory"])
                            if raw.get("memory") else None
                        ),
                        skills=(
                            SkillsConfig(roots=raw["skills"]["roots"])
                            if raw.get("skills") else None
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
