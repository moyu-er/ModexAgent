from __future__ import annotations

from xml.sax.saxutils import escape as _xml_escape

from framework.core.experience.models import ExperienceSummary


def _attr(v: str) -> str:
    return _xml_escape(v).replace('"', "&quot;")


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
                f'name="{_attr(exp.name)}"',
                f'directory="{_attr(exp.directory)}"',
            ]
            if exp.tags:
                attrs.append(f'tags="{_attr(",".join(exp.tags))}"')
            if exp.scenario:
                attrs.append(f'scenario="{_attr(exp.scenario)}"')
            parts.append(f'  <experience {" ".join(attrs)}>')
            if exp.description:
                parts.append(f"    <description>{_xml_escape(exp.description)}</description>")
            parts.append("  </experience>")
        parts.append("</available_experiences>")
        return "\n".join(parts)
