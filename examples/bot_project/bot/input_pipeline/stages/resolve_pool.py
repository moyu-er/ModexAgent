"""S5: resolve pool + agent for the conversation, fill envelope metadata.

Also persists an explicit UI pool choice into PoolSessionStore so PoolRouter
routes the conversation to the right pool. This makes S5 the single owner of
pool resolution + persistence (the WebUI entry no longer resolves inline).
"""

from __future__ import annotations

from enum import StrEnum

from bot.input_pipeline.context import BotInputContext
from modex_agent.core.session_id import SessionInfo, encode_snowflake, session_id_prefix_of
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult


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
    WORKSPACE = "workspace"


def conversation_session_prefix(envelope: UserInputEnvelope, ctx: BotInputContext) -> str:
    """Agent-independent conversation identity used as the pool-store key.

    The pool must be resolved BEFORE the agent is known, so the pool store
    cannot key on the full ``{prefix}.{agent}`` session id. The prefix
    alone is stable across pool switches for one conversation.

    - WebUI (``pre_resolved_session`` set): the prefix is the segment
      before the first ``.`` of the established session id.
    - IM: ``encode_snowflake(external_id)`` via the factory encoding.
    """
    if envelope.pre_resolved_session is not None:
        return session_id_prefix_of(str(envelope.pre_resolved_session))
    return encode_snowflake(envelope.external_id)


def resolve_session_routing(
    envelope: UserInputEnvelope, ctx: BotInputContext
) -> tuple[str, str, str]:
    """Read-only pool/agent/full_session_id resolution.

    Shared by S5 and S3 (S3 needs full_session_id to target CANCEL_TURN before
    S5 runs in pipeline order). Pure function over ctx — no side effects.

    The pool store keys by the agent-independent prefix (see
    :func:`conversation_session_prefix`); the transcript / delta-queue key is the
    full ``session.session_id``. When the upstream channel already established a
    session (``envelope.pre_resolved_session``), it is reused verbatim — this
    prevents the WebUI from double-encoding the prefix.
    """
    session_prefix = conversation_session_prefix(envelope, ctx)
    pool = envelope.explicit_pool or ctx.pool_session_store.get(
        session_prefix, ctx.default_pool
    )
    if envelope.pre_resolved_session is not None:
        session: SessionInfo = envelope.pre_resolved_session
        agent = session.agent_name
    else:
        agent = ctx.agent_for_pool(pool)
        session = ctx.session_factory.create(
            agent_name=agent, external_id=envelope.external_id
        )
    return pool, agent, session.session_id


class ResolvePoolStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        pool, agent, full_sid = resolve_session_routing(envelope, ctx)
        # Always persist the resolved pool mapping so PoolRouter routes
        # correctly, whether the pool was explicitly chosen (WebUI dropdown) or
        # resolved from fallback. Without this, a session created in a non-main
        # pool silently defaults to "main" on every subsequent turn.
        session_prefix = conversation_session_prefix(envelope, ctx)
        ctx.pool_session_store.set(session_prefix, pool)
        envelope.metadata[RoutingMeta.RESOLVED_POOL] = pool
        envelope.metadata[RoutingMeta.RESOLVED_AGENT] = agent
        envelope.metadata[RoutingMeta.FULL_SESSION_ID] = full_sid
        return Continue(value=envelope)
