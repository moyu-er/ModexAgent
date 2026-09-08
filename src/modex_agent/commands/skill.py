"""The consumer-owned Skill resolver seam (plan §11.1).

``commands`` is a lower-level package than the bundled Skills Capability
(``plugins/defaults/capabilities/skills``), so the narrow interface both
inbound adapters depend on lives HERE: the framework ``SkillCommandHandler``
and a business InputStage consume :class:`SkillResolver` and never import
the bundled implementation (dependency direction ``plugins -> commands``).

``arguments`` has one canonical meaning everywhere: the text after the
``/skill-name`` token, with surrounding whitespace removed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict

from modex_agent.core import ContentFormat


class ResolvedSkillCommand(BaseModel):
    """A fully resolved ``/skill-name args`` invocation (plan §11.1).

    Carries the canonical Skill name, the rendered XML user-content (the
    single source of truth is the package's ``builder.build_skill_command_xml``
    — both onramps produce byte-identical XML through it), the on-disk
    location (``None`` for in-memory skills), the content format for the
    user message, and the truncatable message paths.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str
    xml: str
    skill_location: str | None = None
    content_format: ContentFormat = ContentFormat.XML
    truncatable_paths: tuple[str, ...] = ("user_input",)


class SkillResolver(ABC):
    """Resolve a slash skill command against one agent's skill catalog.

    The only bound-resolver contract: ``SkillsSupply.resolver_for`` is the
    sole factory; both command onramps receive references to resolvers
    created by the same supply (plan §11.6).
    """

    @abstractmethod
    async def resolve_command(
        self,
        name: str,
        arguments: str,
    ) -> ResolvedSkillCommand | None:
        """Return the resolved command, or ``None`` when no such skill.

        ``arguments`` is the canonical text after the ``/name`` token
        (surrounding whitespace stripped) — never the raw slash input.
        """
        ...
