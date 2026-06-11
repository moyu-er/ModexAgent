from __future__ import annotations

from framework.core.experience.models import ExperienceSummary
from framework.utils.xml import xml_attr, xml_text


class ExperiencePromptBuilder:
    """Render experiences as compact XML for system prompt injection.

    Only metadata is injected — name, description, tags, scenario, and
    the directory path.  Usage instructions live in the ``experience``
    tool description (not duplicated here).
    """

    def build(self, experiences: list[ExperienceSummary]) -> str:
        if not experiences:
            return ""

        parts = [
            "## Experiences",
            "",
            "Saved problem-solving patterns from past sessions.  Use the "
            "**experience** tool (action=read/write/edit/list/rename) to "
            "manage them.",
            "",
            "<available_experiences>",
        ]
        for exp in experiences:
            attrs = [
                f'name="{xml_attr(exp.name)}"',
                f'directory="{xml_attr(exp.directory)}"',
            ]
            if exp.tags:
                attrs.append(f'tags="{xml_attr(",".join(exp.tags))}"')
            if exp.scenario:
                attrs.append(f'scenario="{xml_attr(exp.scenario)}"')
            parts.append(f"  <experience {' '.join(attrs)}>")
            if exp.description:
                parts.append(f"    <description>{xml_text(exp.description)}</description>")
            parts.append("  </experience>")
        parts.append("</available_experiences>")
        return "\n".join(parts)
