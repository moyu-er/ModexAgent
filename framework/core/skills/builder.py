from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from framework.utils.xml import xml_attr, xml_text

from .models import ResolutionContext, Skill


class SkillPromptBuilder(ABC):
    """Strategy for converting a list of skills into a prompt section."""

    @abstractmethod
    async def build(self, skills: list[Skill], context: ResolutionContext | None = None) -> str:
        """Return the skills prompt section (may be empty)."""


def _render_skill_xml(skills: list[Skill]) -> str:
    """Render skills as compact XML — metadata only, no body content.

    Only name, directory path, and description are included.
    The LLM is expected to ``read`` the SKILL.md for full instructions.
    """
    parts: list[str] = [
        "## Skills",
        "",
        "Skills are reusable agent capabilities — each skill is a self-contained "
        "module with instructions, scripts, and references that extend what you can "
        "do. The XML block below lists installed skills with their name, directory, "
        "and a short description. **The XML only carries metadata, not the full "
        "instructions.** To use a skill, first read its `SKILL.md` file (e.g., via "
        "a file-reading tool pointed at the skill's directory), then follow the "
        "instructions exactly.",
        "",
        "<available_skills>",
    ]
    _max_skill_desc_chars = 400

    for skill in skills:
        dir_path = str(Path(skill.location).parent.resolve()) if skill.location else ""
        parts.append(f'  <skill name="{xml_attr(skill.name)}" directory="{xml_attr(dir_path)}">')
        if skill.description:
            desc = skill.description
            if len(desc) > _max_skill_desc_chars:
                desc = desc[:_max_skill_desc_chars] + "..."
            parts.append(f"    <description>{xml_text(desc)}</description>")
        parts.append("  </skill>")
    parts.append("</available_skills>")
    return "\n".join(parts)


class DefaultSkillBuilder(SkillPromptBuilder):
    """Emit skill metadata as compact XML — never inline full content."""

    def __init__(self, base_path: Path | None = None) -> None:
        _ = base_path

    async def build(self, skills: list[Skill], context: ResolutionContext | None = None) -> str:
        if not skills:
            return ""
        return _render_skill_xml(skills)
