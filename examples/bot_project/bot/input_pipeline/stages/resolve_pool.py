"""S5: resolve pool + agent for the conversation, fill envelope metadata.

Also persists an explicit UI pool choice into PoolSessionStore so PoolRouter
routes the conversation to the right pool. This makes S5 the single owner of
pool resolution + persistence (the WebUI entry no longer resolves inline).
"""

from __future__ import annotations

from enum import StrEnum

from bot.input_pipeline.context import BotInputContext
from framework.input_pipeline.envelope import UserInputEnvelope
from framework.input_pipeline.stage import Continue, InputStage, StageResult


class RoutingMeta(StrEnum):
    """Cross-stage metadata keys passed through ``envelope.metadata``.

    Single source of truth for the routing contract between S3/S5 (producers)
    and S6/S7/S8 (consumers). Using an enum keeps the keys checkable and avoids
    silent typos that would surface only as a runtime KeyError.
    """

    RESOLVED_POOL = "resolved_pool"
    RESOLVED_AGENT = "resolved_agent"
    FULL_SESSION_ID = "full_session_id"
    SKILL_XML = "skill_xml"
    SKILL_NAME = "skill_name"


def resolve_session_routing(
    envelope: UserInputEnvelope, ctx: BotInputContext
) -> tuple[str, str, str]:
    """Read-only pool/agent/full_session_id resolution.

    Shared by S5 and S3 (S3 needs full_session_id to target CANCEL_TURN before
    S5 runs in pipeline order). Pure function over ctx — no side effects.

    Pool lookup first uses the raw *conversation_id* (backward-compatible key).
    After creating the ``SessionId`` object, the pool mapping is re-keyed by
    the full ``str(session)`` so PoolRouter and S5 stay consistent.
    """
    pool = envelope.explicit_pool or ctx.pool_session_store.get(
        envelope.conversation_id, ctx.default_pool
    )
    agent = ctx.agent_for_pool(pool)
    session = ctx.session_factory.create(
        agent_name=agent, external_id=envelope.conversation_id
    )
    return pool, agent, str(session)


class ResolvePoolStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        pool, agent, full_sid = resolve_session_routing(envelope, ctx)
        # Persist an explicit UI pool choice so PoolRouter routes correctly.
        # Idempotent; no-op for IM (explicit_pool is None — reads existing).
        if envelope.explicit_pool:
            ctx.pool_session_store.set(full_sid, pool)
        envelope.metadata[RoutingMeta.RESOLVED_POOL] = pool
        envelope.metadata[RoutingMeta.RESOLVED_AGENT] = agent
        envelope.metadata[RoutingMeta.FULL_SESSION_ID] = full_sid
        return Continue(value=envelope)
