"""S5: resolve pool + agent for the conversation, fill envelope metadata.

Persists only an explicit UI pool choice — or the FIRST implicit-default
resolution of a brand-new conversation — into PoolSessionStore so inferred
tree ownership can never rewrite prefix routing (peer sessions reuse a
foreign prefix; pinning their attribution would contaminate the owning
conversation's route). This makes S5 the single owner of pool resolution +
explicit-choice/first-default persistence.
"""

from __future__ import annotations

from enum import StrEnum

from anyio.to_thread import run_sync
from pydantic import BaseModel, ConfigDict

from bot.input_pipeline.context import BotInputContext
from modex_agent.core.session_id import SessionInfo, encode_snowflake, session_id_prefix_of
from modex_agent.input_pipeline.context import InputContext
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
    SKILL_LOCATION = "skill_location"
    SKILL_CONTENT_FORMAT = "skill_content_format"
    SKILL_TRUNCATABLE_PATHS = "skill_truncatable_paths"
    WORKSPACE = "workspace"
    APPROVAL_DECISION = "approval_decision"
    MODEL_PROVIDER = "model_provider"
    MODEL_MODEL = "model_model"
    RESOLVED_MODEL = "resolved_model"
    TREE_RESOLVED_POOL = "tree_resolved_pool"
    PREPARED_MESSAGE = "prepared_message"


class SelectionSource(StrEnum):
    """Where a resolution got its pool from (S5 routing contract).

    Governs persistence: only EXPLICIT user intent and the first
    IMPLICIT_DEFAULT of a genuinely fresh conversation may write the routing
    table. TREE attribution (session-tree ownership — peer sessions reuse a
    foreign conversation prefix) and STORED routes never write, per the
    attribution/routing split in bot/service/AGENTS.md.
    """

    EXPLICIT = "explicit"
    TREE = "tree"
    STORED = "stored"
    IMPLICIT_DEFAULT = "implicit_default"


class SessionRouting(BaseModel):
    """Read-only pool/agent/session resolution result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pool: str | None
    agent: str
    full_session_id: str
    source: SelectionSource


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
) -> SessionRouting:
    """Read-only pool/agent/full_session_id resolution + selection source.

    Shared by S5 and S3 (S3 needs full_session_id to target CANCEL_TURN before
    S5 runs in pipeline order). Pure function over ctx — no side effects.

    The pool store keys by the agent-independent prefix (see
    :func:`conversation_session_prefix`); the transcript / delta-queue key is the
    full ``session.session_id``. When the upstream channel already established a
    session (``envelope.pre_resolved_session``), it is reused verbatim — this
    prevents the WebUI from double-encoding the prefix.

    Returns a :class:`SessionRouting`; ``pool`` is ``None`` when no routable
    pool exists (no explicit pool, no tree attribution, no stored mapping,
    and no default pool) — the caller (``ResolvePoolStage``) terminates with
    ``pool_unavailable`` in that case. ``source`` tells S5 whether the
    resolution may pin (EXPLICIT / first IMPLICIT_DEFAULT of a fresh
    conversation) or must not (TREE attribution, STORED route).
    """
    session_prefix = conversation_session_prefix(envelope, ctx)
    stored_pool = ctx.pool_session_store.get_pool(session_prefix)
    tree_pool = envelope.metadata.get(RoutingMeta.TREE_RESOLVED_POOL)
    if envelope.explicit_pool is not None:
        pool: str | None = envelope.explicit_pool
        source = SelectionSource.EXPLICIT
    elif tree_pool is not None:
        pool = tree_pool
        source = SelectionSource.TREE
    elif stored_pool is not None:
        pool = stored_pool
        source = SelectionSource.STORED
    else:
        pool = ctx.default_pool
        source = SelectionSource.IMPLICIT_DEFAULT
    if pool is None:
        # No routable pool — caller terminates. Agent/session are moot.
        return SessionRouting(pool=None, agent="", full_session_id="", source=source)
    if envelope.pre_resolved_session is not None:
        session: SessionInfo = envelope.pre_resolved_session
        agent = session.agent_name
    else:
        agent = ctx.agent_for_pool(pool)
        session = ctx.session_factory.create(
            agent_name=agent, external_id=envelope.external_id
        )
    return SessionRouting(pool=pool, agent=agent, full_session_id=session.session_id, source=source)


class ResolvePoolStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: InputContext
    ) -> StageResult:
        assert isinstance(ctx, BotInputContext), "Bot input stages require BotInputContext"
        available = ctx.available_pools()
        if not available:
            return Terminate(
                reason="no_pool_configured",
                response={
                    "message": "No pool is configured. Please create a pool in the settings first."
                },
            )
        routing = resolve_session_routing(envelope, ctx)
        if routing.pool is None or routing.pool not in available:
            return Terminate(
                reason="pool_unavailable",
                response={
                    "message": f"Pool '{routing.pool}' is not available. It may have been removed. Please select a different pool."
                },
            )
        if routing.source in (SelectionSource.EXPLICIT, SelectionSource.IMPLICIT_DEFAULT):
            # Persistence contract (PA-07 + attribution invariant):
            # - EXPLICIT user intent persists as always;
            # - IMPLICIT_DEFAULT pins the FIRST default of a genuinely fresh
            #   conversation (no stored route) so later preference changes
            #   re-route only NEW conversations;
            # - TREE attribution NEVER lands in the routing table (peer
            #   sessions reuse a foreign prefix — pinning them would
            #   contaminate the owning conversation's route);
            # - STORED routes are never rewritten.
            session_prefix = conversation_session_prefix(envelope, ctx)
            await run_sync(ctx.pool_session_store.set, session_prefix, routing.pool)
        envelope.metadata[RoutingMeta.RESOLVED_POOL] = routing.pool
        envelope.metadata[RoutingMeta.RESOLVED_AGENT] = routing.agent
        envelope.metadata[RoutingMeta.FULL_SESSION_ID] = routing.full_session_id
        return Continue(value=envelope)
