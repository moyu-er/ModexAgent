"""Skill prompt builders + the command-invocation XML authority (plan §11.5).

``build_skill_command_xml`` is the single source of truth for the
``/skillName args`` XML user-content shape — BOTH command onramps (the
framework ``SkillCommandHandler`` and the business InputStage resolver
adapter) produce byte-identical XML through it (plan §5.3 correction).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from pathlib import Path

from modex_agent.utils.xml import xml_attr, xml_text

from .models import ResolutionContext, Skill


class SkillPromptBuilder(ABC):
    """Strategy for converting a list of skills into a prompt section."""

    @abstractmethod
    async def build(
        self, skills: Sequence[Skill], context: ResolutionContext | None = None
    ) -> str:
        """Return the skills prompt section (may be empty)."""


def _render_skill_xml(skills: Sequence[Skill]) -> str:
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
        dir_path = str(Path(skill.location).parent) if skill.location else ""
        parts.append(f'  <skill name="{xml_attr(skill.name)}" directory="{xml_attr(dir_path)}">')
        if skill.description:
            desc = skill.description
            if len(desc) > _max_skill_desc_chars:
                desc = desc[:_max_skill_desc_chars] + "..."
            parts.append(f"    <description>{xml_text(desc)}</description>")
        parts.append("  </skill>")
    parts.append("</available_skills>")
    return "\n".join(parts)


def build_skill_command_xml(
    skill_name: str,
    skill_content: str,
    user_args: str,
    skill_location: str | None = None,
) -> str:
    """Render a ``/skillName args`` invocation as XML user-content.

    Single source of truth for the command-invocation skill format.  Used by
    both the framework command processor (``SkillCommandHandler``) and the
    input-pipeline skill stage so the two paths produce identical output.

    The skill body is inlined verbatim (escaped) under ``<skill>``; the user's
    arguments follow under ``<user_input>``.  When *skill_location* is given
    (the on-disk path to the ``SKILL.md`` file), a ``directory`` attribute is
    added pointing at the skill's parent directory — the same convention the
    system-prompt ``<available_skills>`` block uses, so the LLM can resolve
    relative file references (e.g. a sibling ``GLOSSARY.md``) inside the body.
    Omitted when *skill_location* is ``None`` (in-memory skills with no
    on-disk files to reference).
    """
    dir_attr = ""
    if skill_location:
        dir_path = str(Path(skill_location).parent)
        dir_attr = f' directory="{xml_attr(dir_path)}"'
    return (
        f'<command_context type="skill" name="{xml_attr(skill_name)}"{dir_attr}>\n'
        f"<skill>\n{xml_text(skill_content)}\n</skill>\n"
        f"</command_context>\n\n"
        f"<user_input>\n{xml_text(user_args)}\n</user_input>"
    )


class DefaultSkillBuilder(SkillPromptBuilder):
    """Emit skill metadata as compact XML — never inline full content."""

    async def build(
        self, skills: Sequence[Skill], context: ResolutionContext | None = None
    ) -> str:
        if not skills:
            return ""
        return _render_skill_xml(skills)
