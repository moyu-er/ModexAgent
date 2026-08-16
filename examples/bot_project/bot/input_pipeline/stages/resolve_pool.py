"""S5: resolve pool + agent for the conversation, fill envelope metadata.

Persists only an explicit UI pool choice into PoolSessionStore so inferred
tree ownership cannot rewrite prefix routing. This makes S5 the single owner
of pool resolution + explicit-choice persistence.
"""

from __future__ import annotations

from enum import StrEnum

from anyio.to_thread import run_sync

from bot.input_pipeline.context import BotInputContext
from modex_agent.core.session_id import SessionInfo, encode_snowflake, session_id_prefix_of
from modex_agent.input_pipeline.envelope import UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult, Terminate


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
    APPROVAL_DECISION = "approval_decision"
    MODEL_PROVIDER = "model_provider"
    MODEL_MODEL = "model_model"
    RESOLVED_MODEL = "resolved_model"
    TREE_RESOLVED_POOL = "tree_resolved_pool"


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
) -> tuple[str | None, str, str]:
    """Read-only pool/agent/full_session_id resolution.

    Shared by S5 and S3 (S3 needs full_session_id to target CANCEL_TURN before
    S5 runs in pipeline order). Pure function over ctx — no side effects.

    The pool store keys by the agent-independent prefix (see
    :func:`conversation_session_prefix`); the transcript / delta-queue key is the
    full ``session.session_id``. When the upstream channel already established a
    session (``envelope.pre_resolved_session``), it is reused verbatim — this
    prevents the WebUI from double-encoding the prefix.

    Returns ``pool=None`` when no routable pool exists (no explicit pool, no
    tree attribution, no stored mapping, and no default pool). The caller
    (``ResolvePoolStage``) terminates with ``pool_unavailable`` in that case.
    """
    session_prefix = conversation_session_prefix(envelope, ctx)
    pool = envelope.explicit_pool or envelope.metadata.get(
        RoutingMeta.TREE_RESOLVED_POOL
    )
    if pool is None:
        pool = ctx.pool_session_store.get(session_prefix, ctx.default_pool)
    if pool is None:
        # No routable pool — caller terminates. Agent/session are moot.
        return None, "", ""
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
        available = ctx.available_pools()
        if not available:
            return Terminate(
                reason="no_pool_configured",
                response={
                    "message": "No pool is configured. Please create a pool in the settings first."
                },
            )
        pool, agent, full_sid = resolve_session_routing(envelope, ctx)
        if pool is None or pool not in available:
            return Terminate(
                reason="pool_unavailable",
                response={
                    "message": f"Pool '{pool}' is not available. It may have been removed. Please select a different pool."
                },
            )
        if envelope.explicit_pool:
            session_prefix = conversation_session_prefix(envelope, ctx)
            await run_sync(ctx.pool_session_store.set, session_prefix, pool)
        envelope.metadata[RoutingMeta.RESOLVED_POOL] = pool
        envelope.metadata[RoutingMeta.RESOLVED_AGENT] = agent
        envelope.metadata[RoutingMeta.FULL_SESSION_ID] = full_sid
        return Continue(value=envelope)
