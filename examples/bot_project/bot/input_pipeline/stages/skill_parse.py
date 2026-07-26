"""S6: identify /skillName commands, validate against a registry, convert to XML.

This stage claims and resolves /skillName for the resolved pool. It NEVER
rejects: an unregistered command passes through, and the terminal
UnsupportedCommandStage decides what to tell the user. Any stage that emits
a "not supported" notice belongs behind a single terminal stage, not here.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.core.skills.builder import build_skill_command_xml
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult
from modex_agent.multi_agent.pool_instance import PoolInstance


@dataclass
class ParsedSkill:
    name: str
    raw: str
    xml_form: str


class SkillRegistry(ABC):
    """Per-pool skill registry.

    Each pool has its own set of skills (configured via skills/{pool}/{agent}/).
    The registry resolves the correct SkillManager for a given pool name and
    returns the XML form for the LLM, or ``None`` when the name is unknown.

    A single ``resolve`` call (rather than separate ``exists`` + ``parse``)
    avoids resolving the skill twice — important because resolution may scan
    skill directories for freshness.
    """

    @abstractmethod
    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        """Return the parsed skill for *name* in *pool*, or ``None`` if unknown."""
        ...


class PoolSkillManagerRegistry(SkillRegistry):
    """Concrete registry backed by each pool's real ``SkillManager``.

    The XML form reuses the framework's ``build_skill_command_xml`` (the same
    path as ``SkillCommandHandler``), so there is one source of truth for the
    skill-command XML shape.
    """

    def __init__(self, pools: Mapping[str, PoolInstance]) -> None:
        self._pools = pools

    async def resolve(self, pool: str, name: str, content: str) -> ParsedSkill | None:
        pi = self._pools.get(pool)
        if pi is None or pi.skill_manager is None:
            return None
        skill = await pi.skill_manager.get_skill(name)
        if skill is None:
            return None
        return ParsedSkill(
            name=skill.name,
            raw=content,
            xml_form=build_skill_command_xml(skill.name, skill.content, content, skill.location),
        )


class SkillParseStage(InputStage):
    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    async def process(self, envelope: UserInputEnvelope, ctx: BotInputContext) -> StageResult:
        content = (envelope.content or "").strip()
        if not content.startswith("/"):
            return Continue(value=envelope)

        command_name = content[1:].split(None, 1)[0].lower()

        # Skills are per-pool: S5 resolved the pool onto the envelope.
        resolved_pool = str(envelope.metadata.get(RoutingMeta.RESOLVED_POOL, ctx.default_pool))
        parsed = await self._registry.resolve(resolved_pool, command_name, content)
        if parsed is None:
            # Not a skill. Do NOT reject here — pass through so the terminal
            # UnsupportedCommandStage gives the single generic notice.
            return Continue(value=envelope)

        envelope.metadata[RoutingMeta.SKILL_XML] = parsed.xml_form
        envelope.metadata[RoutingMeta.SKILL_NAME] = command_name
        envelope.command_status = CommandStatus.RESOLVED
        return Continue(value=envelope)
