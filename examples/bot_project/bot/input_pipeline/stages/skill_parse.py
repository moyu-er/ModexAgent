"""S6: identify /skillName commands, validate against a registry, convert to XML.

IM: reserved control commands (/cd /pool /exit /stop /pwd) are intercepted by
S2/S3 and never reach this stage.  Only registered skills pass through.

WebUI: S2/S3 are absent, so builtin commands (/cd /exit /pwd) and pool-switch
commands (/pool_name) reach S6.  They are rejected with a user-facing notice
and are NOT persisted (the pipeline terminates before S7).

Skill resolution is per-pool: S5 resolves the pool and stores it in
``envelope.metadata[RoutingMeta.RESOLVED_POOL]``.  S6 uses that pool to look
up the correct SkillManager (via the SkillRegistry), so each pool sees only
its own skills.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from bot.service.pool_instance import PoolInstance
from modex_agent.commands.constants import BuiltinCommand
from modex_agent.core.skills.builder import build_skill_command_xml
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult, Terminate

# The set of builtin command values that S2/S3 handle for IM channels.
# In WebUI they reach S6 because those stages are skipped.
_BUILTIN_VALUES: frozenset[str] = frozenset(c.value for c in BuiltinCommand)


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
    async def resolve(
        self, pool: str, name: str, content: str
    ) -> ParsedSkill | None:
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

    async def resolve(
        self, pool: str, name: str, content: str
    ) -> ParsedSkill | None:
        pi = self._pools.get(pool)
        if pi is None or pi.skill_manager is None:
            return None
        skill = await pi.skill_manager.get_skill(name)
        if skill is None:
            return None
        return ParsedSkill(
            name=skill.name,
            raw=content,
            xml_form=build_skill_command_xml(skill.name, skill.content, content),
        )


class SkillParseStage(InputStage):
    def __init__(
        self,
        registry: SkillRegistry,
        *,
        known_pools: set[str] | None = None,
    ) -> None:
        self._registry = registry
        self._known_pools: frozenset[str] = frozenset(known_pools or ())

    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        content = (envelope.content or "").strip()
        if not content.startswith("/"):
            return Continue(value=envelope)

        command_name = content[1:].split(None, 1)[0].lower()

        # Builtin commands (/cd /exit /pwd /approve /deny /continue)
        # ─────────────────────────────────────────────────────────────
        # WebUI: rejected here with "builtin_not_supported" — intentional.
        #   The WebUI has no S2/S3 (control-command stages); /pwd /cd /exit
        #   are unnecessary because the workspace panel and sidebar controls
        #   provide the same functionality visually.  DO NOT add WebUI-side
        #   interception (e.g. _try_intercept_control) in _ws_send_message.
        #
        # IM (QQ, etc.): intercepted by S2 (EnvironmentControlStage) via
        #   ctx.command_adapter._try_intercept_control BEFORE reaching S6.
        #   IM clients need these commands because they lack a graphical
        #   workspace/switching interface.
        if command_name in _BUILTIN_VALUES:
            return Terminate(
                reason="builtin_not_supported",
                response={
                    "message": (
                        f"Command '/{command_name}' is not supported here. "
                        f"Use the workspace panel or sidebar controls instead."
                    ),
                },
            )

        # Pool-switch commands (/coding, /main, etc.) — handled by S2 in IM,
        # but S2 is absent from the WebUI pipeline.  Surface a helpful message
        # so the user knows to use the pool selector instead.
        if command_name in self._known_pools:
            return Terminate(
                reason="pool_not_supported",
                response={
                    "message": (
                        f"Pool switch '/{command_name}' is not supported here. "
                        f"Use the pool selector in the sidebar instead."
                    ),
                },
            )

        # Resolve the pool that S5 set on the envelope.  Skills are per-pool.
        resolved_pool = str(
            envelope.metadata.get(RoutingMeta.RESOLVED_POOL, ctx.default_pool)
        )

        parsed = await self._registry.resolve(resolved_pool, command_name, content)
        if parsed is None:
            return Terminate(
                reason="unrecognized_command",
                response={"message": f"Command '/{command_name}' not recognized"},
            )

        envelope.metadata[RoutingMeta.SKILL_XML] = parsed.xml_form
        envelope.metadata[RoutingMeta.SKILL_NAME] = command_name
        return Continue(value=envelope)
