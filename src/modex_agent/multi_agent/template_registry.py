"""AgentTemplateRegistry — scans and loads per-pool subagent templates."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from modex_agent.ioc.configs.agent import ExperienceConfig
from modex_agent.ioc.configs.approval import ApprovalConfig
from modex_agent.ioc.configs.memory import MemoryConfig
from modex_agent.multi_agent.template import AgentTemplate
from modex_agent.tools.presets import (
    DEFAULT_FORK_MAX_MESSAGES,
    ContextMode,
    SystemPromptMode,
    ToolPreset,
    ToolSupplement,
)

logger = logging.getLogger(__name__)


def _parse_tool_supplements(raw: list) -> list[ToolSupplement]:
    out: list[ToolSupplement] = []
    for item in raw or []:
        try:
            out.append(ToolSupplement(item))
        except ValueError:
            logger.warning("Invalid tool_supplement '%s'; skipping", item)
    return out


class AgentTemplateRegistry:
    """Scans config/pools/*/templates/*.yml and loads AgentTemplate definitions.

    Templates are isolated by pool_name — a template only exists within
    the pool directory it's defined in.
    """

    # Accepted YAML keys — the full AgentTemplate field set as written in
    # template files. Any other key is a typo / stale field and must surface
    # (extra="forbid" semantics for the manually-parsed dataclass).
    # ``memory`` is deliberately NOT accepted here: subagent memory is baked
    # (sub-minimal, immutable, spec §9). A template carrying a ``memory:`` block
    # is rejected so a stale/hand-edited rich-memory block can never silently
    # override the baked preset. ``skills`` likewise (disk-only, not in YAML).
    _ACCEPTED_KEYS: frozenset[str] = frozenset({
        "agent_name", "description", "max_steps", "tool_preset",
        "tool_supplements", "context_mode",
        "system_prompt_mode", "fork_max_messages", "mcp",
        "approval", "experience",
    })

    def __init__(
        self,
        project_dir: Path,
        *,
        default_subagent_memory: MemoryConfig | None = None,
    ) -> None:
        """Init.

        ``default_subagent_memory`` is baked onto EVERY subagent template,
        unconditionally (spec §9 — sub-minimal, immutable). A template may NOT
        carry its own ``memory:`` block; the caller's factory is the single
        source of truth.
        """
        self._default_memory = default_subagent_memory
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
                    if not raw or "agent_name" not in raw:
                        logger.warning("Skipping invalid template: %s", yml_path)
                        continue

                    # Reject unknown keys (extra="forbid" semantics for the
                    # manually-parsed dataclass). A typo'd key (e.g. agent_typ:)
                    # surfaces as a ValueError logged with full context below.
                    unknown = set(raw.keys()) - self._ACCEPTED_KEYS
                    if unknown:
                        raise ValueError(
                            f"Unknown template key(s) {sorted(unknown)} in "
                            f"{yml_path}; accepted keys: {sorted(self._ACCEPTED_KEYS)}"
                        )

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

                    system_prompt_mode_raw = raw.get(
                        "system_prompt_mode", SystemPromptMode.REPLACE.value
                    )
                    try:
                        system_prompt_mode = SystemPromptMode(system_prompt_mode_raw)
                    except ValueError:
                        logger.warning(
                            "Invalid system_prompt_mode '%s' in %s, falling back to %s",
                            system_prompt_mode_raw,
                            yml_path,
                            SystemPromptMode.REPLACE.value,
                        )
                        system_prompt_mode = SystemPromptMode.REPLACE

                    fork_max_messages = raw.get(
                        "fork_max_messages", DEFAULT_FORK_MAX_MESSAGES
                    )
                    if (
                        isinstance(fork_max_messages, bool)
                        or not isinstance(fork_max_messages, int)
                        or fork_max_messages < 1
                    ):
                        fork_max_messages = DEFAULT_FORK_MAX_MESSAGES

                    template = AgentTemplate(
                        agent_name=raw["agent_name"],
                        description=raw.get("description", ""),
                        max_steps=raw.get("max_steps", 80),
                        tool_preset=tool_preset,
                        tool_supplements=_parse_tool_supplements(raw.get("tool_supplements")),
                        context_mode=context_mode,
                        system_prompt_mode=system_prompt_mode,
                        fork_max_messages=fork_max_messages,
                        mcp=list(raw.get("mcp") or []),
                        memory=self._default_memory,
                        approval=(
                            ApprovalConfig.model_validate(raw["approval"])
                            if raw.get("approval")
                            else None
                        ),
                        experience=(
                            ExperienceConfig.model_validate(raw["experience"])
                            if raw.get("experience")
                            else None
                        ),
                    )
                    self._templates[pool_name][template.agent_name] = template
                    logger.debug("Loaded template %s for pool %s", template.agent_name, pool_name)
                except Exception:
                    logger.exception("Failed to load template: %s", yml_path)

    def list_templates(self, pool_name: str) -> list[AgentTemplate]:
        return list(self._templates.get(pool_name, {}).values())

    def get_template(self, pool_name: str, agent_name: str) -> AgentTemplate | None:
        return self._templates.get(pool_name, {}).get(agent_name)
