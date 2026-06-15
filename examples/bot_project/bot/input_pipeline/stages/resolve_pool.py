"""S5: resolve pool + agent for the conversation, fill envelope metadata.

Also persists an explicit UI pool choice into PoolSessionStore so PoolRouter
routes the conversation to the right pool. This makes S5 the single owner of
pool resolution + persistence (the WebUI entry no longer resolves inline).
"""

from __future__ import annotations

from enum import StrEnum

from bot.input_pipeline.context import BotInputContext
from framework.core.session_id import SessionInfo, encode_snowflake, snowflake_of
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


def conversation_snowflake(envelope: UserInputEnvelope, ctx: BotInputContext) -> str:
    """Agent-independent conversation identity used as the pool-store key.

    The pool must be resolved BEFORE the agent is known, so the pool store
    cannot key on the full ``{snowflake}.{agent}`` session id. The snowflake
    alone is stable across pool switches for one conversation.

    - WebUI (``pre_resolved_session`` set): the snowflake is the segment
      before the first ``.`` of the established session id.
    - IM: ``encode_snowflake(conversation_id)`` via the factory encoding.
    """
    if envelope.pre_resolved_session is not None:
        return snowflake_of(str(envelope.pre_resolved_session))
    return encode_snowflake(envelope.conversation_id)


def resolve_session_routing(
    envelope: UserInputEnvelope, ctx: BotInputContext
) -> tuple[str, str, str]:
    """Read-only pool/agent/full_session_id resolution.

    Shared by S5 and S3 (S3 needs full_session_id to target CANCEL_TURN before
    S5 runs in pipeline order). Pure function over ctx — no side effects.

    The pool store keys by the agent-independent snowflake (see
    :func:`conversation_snowflake`); the transcript / delta-queue key is the
    full ``str(session)``. When the upstream channel already established a
    session (``envelope.pre_resolved_session``), it is reused verbatim — this
    prevents the WebUI from double-encoding the snowflake.
    """
    snowflake = conversation_snowflake(envelope, ctx)
    pool = envelope.explicit_pool or ctx.pool_session_store.get(
        snowflake, ctx.default_pool
    )
    if envelope.pre_resolved_session is not None:
        session: SessionInfo = envelope.pre_resolved_session
        agent = session.agent_name
    else:
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
        # Keyed by the agent-independent snowflake (same key PoolRouter reads).
        if envelope.explicit_pool:
            ctx.pool_session_store.set(conversation_snowflake(envelope, ctx), pool)
        envelope.metadata[RoutingMeta.RESOLVED_POOL] = pool
        envelope.metadata[RoutingMeta.RESOLVED_AGENT] = agent
        envelope.metadata[RoutingMeta.FULL_SESSION_ID] = full_sid
        return Continue(value=envelope)
