"""S6: identify /skillName commands, resolve via the pool's SkillResolver.

This stage claims and resolves /skillName for the resolved pool. It NEVER
rejects: an unregistered command passes through, and the terminal
UnsupportedCommandStage decides what to tell the user.

Onramp convergence (plan §11.1/§11.6): the stage preserves the RAW slash
input in ``envelope.content`` for transcript persistence but passes only
the CANONICAL arguments (text after the command token, surrounding
whitespace stripped) to the shared ``SkillResolver`` — the same
``resolve_command`` contract the framework ``SkillCommandHandler`` uses,
producing byte-identical XML from both onramps. The single consumer-owned
interface lives in ``modex_agent.commands.skill``.
"""

from __future__ import annotations

from collections.abc import Mapping

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.commands.skill import SkillResolver
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult


class PoolSkillResolverRegistry:
    """Per-pool bound-resolver lookup backed by each pool's root resolver.

    The resolvers are created by the pool's ``SkillsSupply``
    (``resolver_for(root_agent_name)`` — plan §11.3.1); this registry only
    LOOKS THEM UP by pool name. A pool without one (vetoed capability,
    external pool) simply resolves no skill commands.
    """

    def __init__(self, resolvers: Mapping[str, SkillResolver | None]) -> None:
        self._resolvers = dict(resolvers)

    def resolver_for_pool(self, pool: str) -> SkillResolver | None:
        return self._resolvers.get(pool)


class SkillParseStage(InputStage):
    def __init__(self, registry: PoolSkillResolverRegistry) -> None:
        self._registry = registry

    async def process(self, envelope: UserInputEnvelope, ctx: BotInputContext) -> StageResult:
        content = (envelope.content or "").strip()
        if not content.startswith("/"):
            return Continue(value=envelope)

        command_name, _, args = content[1:].partition(" ")
        command_name = command_name.lower()
        canonical_args = args.strip()

        # Skills are per-pool: S5 resolved the pool onto the envelope.
        resolved_pool = str(envelope.metadata.get(RoutingMeta.RESOLVED_POOL, ctx.default_pool))
        resolver = self._registry.resolver_for_pool(resolved_pool)
        resolved = None
        if resolver is not None:
            resolved = await resolver.resolve_command(command_name, canonical_args)
        if resolved is None:
            # Not a skill. Do NOT reject here — pass through so the terminal
            # UnsupportedCommandStage gives the single generic notice.
            return Continue(value=envelope)

        envelope.metadata[RoutingMeta.SKILL_XML] = resolved.xml
        envelope.metadata[RoutingMeta.SKILL_NAME] = resolved.skill_name
        envelope.metadata[RoutingMeta.SKILL_LOCATION] = resolved.skill_location or ""
        envelope.metadata[RoutingMeta.SKILL_CONTENT_FORMAT] = resolved.content_format
        envelope.metadata[RoutingMeta.SKILL_TRUNCATABLE_PATHS] = list(
            resolved.truncatable_paths
        )
        envelope.command_status = CommandStatus.RESOLVED
        return Continue(value=envelope)
