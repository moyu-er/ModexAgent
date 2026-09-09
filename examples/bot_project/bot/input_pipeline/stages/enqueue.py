"""S8: build the final InputMessage for delivery.

Channel-agnostic: message construction never touches a physical queue or
WS-specific method.

T07 (DESIGN.md §7): the final-message construction is centralized in
:func:`build_input_message` — the ONE shared builder consumed by both delivery
forms (``BotInputPreparation.handle`` for the sync-callback delivery path and
``BotInputPreparation.prepare`` for the typed path, which never enqueues). The
builder also owns the HANDLED guard: a HANDLED envelope without
a prepared carriage (``RoutingMeta.PREPARED_MESSAGE``, set by
``CommandDispatchStage``) builds nothing — the original quiet-Continue shape.
The carriage is read with ``.get``; the envelope metadata dict is never mutated
or popped here.
"""

from __future__ import annotations

from pathlib import Path

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.messaging.models import InputMessage


def build_input_message(
    envelope: UserInputEnvelope, ctx: BotInputContext
) -> InputMessage | None:
    """Construct the turn's InputMessage — the single shared S8 builder.

    Returns ``None`` for a HANDLED envelope without a prepared carriage
    (nothing was meant to be delivered); otherwise the message to deliver:
    the early-delivery carriage (e.g. ``/continue``) when present, else the
    construction from the resolved routing/skill metadata.
    """
    carriage = envelope.metadata.get(RoutingMeta.PREPARED_MESSAGE)
    if isinstance(carriage, InputMessage):
        return carriage
    if envelope.command_status == CommandStatus.HANDLED:
        return None
    llm_content = envelope.metadata.get(RoutingMeta.SKILL_XML) or envelope.content
    attachments = [a.local_path for a in envelope.attachments if a.local_path]
    # Reuse the session resolved by S5 (already encoded once) instead of
    # re-creating it, so the enqueued session id matches the pool/transcript
    # keys. For the WebUI this honors the pre-resolved session from attach.
    if envelope.pre_resolved_session is not None:
        session = envelope.pre_resolved_session
    else:
        agent = envelope.metadata[RoutingMeta.RESOLVED_AGENT]
        session = ctx.session_factory.create(
            agent_name=agent,
            external_id=envelope.external_id,
            metadata={"channel": envelope.channel},
        )
    # Register the WebUI-resolved model into the cross-broker carrier keyed
    # by session id (ModelChoiceBindHook reads it at turn start). IM path
    # carries no RESOLVED_MODEL and is skipped, falling back to the default.
    resolved_model = envelope.metadata.get(RoutingMeta.RESOLVED_MODEL)
    registry = ctx.model_choice_registry
    if resolved_model is not None and registry is not None:
        registry.set(session.session_id, resolved_model)
    return InputMessage(
        content=llm_content,
        session=session,
        channel=envelope.channel,
        source=envelope.channel,  # PoolRouter uses msg.source for AgentAddress name
        chat_id=envelope.metadata.get("chat_id", ""),  # broker header; never drop to default
        metadata=envelope.metadata,
        attachments=attachments,
        content_format=envelope.metadata.get(RoutingMeta.SKILL_CONTENT_FORMAT),
        truncatable_paths=envelope.metadata.get(
            RoutingMeta.SKILL_TRUNCATABLE_PATHS
        ),
        workspace=Path(envelope.metadata[RoutingMeta.WORKSPACE])
        if RoutingMeta.WORKSPACE in envelope.metadata
        else None,
        approval_decision=envelope.metadata.get(RoutingMeta.APPROVAL_DECISION),
        # Typed carriage: gate-accepted Attachment records reach the turn so
        # preprocess can inject the transient path reference (ADR-0013 §1/§10).
        attachments_resolved=list(envelope.resolved_attachments),
    )
