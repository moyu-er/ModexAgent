from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from .models import ResolutionContext, Skill

logger = logging.getLogger(__name__)


class SkillPromptBuilder(ABC):
    """Strategy for converting a list of skills into a prompt section."""

    @abstractmethod
    async def build(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> str:
        """Return the `# Skills` section content (may be empty)."""


class InlineBuilder(SkillPromptBuilder):
    """Embed the full content of every skill directly into the prompt."""

    async def build(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> str:
        if not skills:
            return ""
        lines = ["## Skills"]
        for skill in skills:
            lines.append(f"### {skill.name}")
            if skill.description:
                lines.append(f"*{skill.description}*")
            lines.append("")
            lines.append(skill.content)
            lines.append("")
        return "\n".join(lines).strip()


DEFAULT_PROGRESSIVE_PROMPT = (
    "You have access to the following skills. "
    "Use a file-reading tool to load the full content of a skill when you need it. "
    "You can also use list_dir or read_file to browse skill resources (scripts, references, assets)."
)


def _render_skill_xml(
    skills: list[Skill],
    prompt_template: str = DEFAULT_PROGRESSIVE_PROMPT,
) -> str:
    """Render skills as a compact XML directory.

    Only name, directory path, and description are included.
    The LLM is expected to ``read_file`` the SKILL.md for full content.
    """
    from xml.sax.saxutils import escape as xml_escape

    parts: list[str] = [
        "## Skills",
        "",
        prompt_template,
        "",
        "<available_skills>",
    ]
    for skill in skills:
        dir_path = str(Path(skill.location).parent.resolve()) if skill.location else ""

        parts.append(
            f'  <skill name="{xml_escape(skill.name)}" '
            f'directory="{xml_escape(dir_path)}">'
        )
        if skill.description:
            parts.append(f'    <description>{xml_escape(skill.description)}</description>')
        parts.append("  </skill>")

    parts.append("</available_skills>")
    return "\n".join(parts).strip()


def _has_read_tool(context: ResolutionContext | None) -> bool:
    """Check whether the context provides a file-reading tool."""
    tm = getattr(context, "tool_manager", None) if context else None
    if tm is None:
        return False
    for name in ("read_file", "filesystem_read_file", "cat"):
        try:
            if hasattr(tm, "has_tool") and tm.has_tool(name):
                return True
        except Exception:
            pass
    return False


class ProgressiveBuilder(SkillPromptBuilder):
    """Emit a compact directory and ask the model to load full content on demand.

    If no file-reading tools are available, silently degrades to ``InlineBuilder``.
    """

    def __init__(
        self,
        base_path: Path | None = None,
        prompt_template: str = DEFAULT_PROGRESSIVE_PROMPT,
    ) -> None:
        self._base_path = base_path
        self._prompt_template = prompt_template

    async def build(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> str:
        if not skills:
            return ""
        if not _has_read_tool(context):
            logger.info("ProgressiveBuilder downgrading to InlineBuilder (no read_file tool)")
            return await InlineBuilder().build(skills, context)
        return _render_skill_xml(skills, self._prompt_template)


class HybridBuilder(SkillPromptBuilder):
    """Inline critical skills and list the rest in a directory table.

    Modes:
    - ``always`` — only ``metadata.always=True`` skills are inlined.
    - ``all`` — all skills are inlined (equivalent to ``InlineBuilder``).
    - ``none`` — all skills are listed (equivalent to ``ProgressiveBuilder``).
    """

    def __init__(
        self,
        inline_mode: str = "always",
        prompt_template: str = DEFAULT_PROGRESSIVE_PROMPT,
    ) -> None:
        self._inline_mode = inline_mode
        self._prompt_template = prompt_template

    async def build(
        self,
        skills: list[Skill],
        context: ResolutionContext | None = None,
    ) -> str:
        if not skills:
            return ""
        if self._inline_mode == "all":
            return await InlineBuilder().build(skills, context)
        if self._inline_mode == "none":
            if not _has_read_tool(context):
                logger.info("HybridBuilder 'none' mode downgrading to InlineBuilder (no read_file tool)")
                return await InlineBuilder().build(skills, context)
            return _render_skill_xml(skills, prompt_template=self._prompt_template)
        # "always" mode
        inline = [s for s in skills if s.metadata.always]
        directory = [s for s in skills if not s.metadata.always]
        parts: list[str] = []
        if inline:
            parts.append(await InlineBuilder().build(inline, context))
        if directory:
            if _has_read_tool(context):
                parts.append(_render_skill_xml(directory, prompt_template=self._prompt_template))
            else:
                logger.info("HybridBuilder 'always' mode downgrading directory to InlineBuilder (no read_file tool)")
                parts.append(await InlineBuilder().build(directory, context))
        return "\n\n".join(parts).strip()
