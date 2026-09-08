"""S8: build the final InputMessage and enqueue it via ctx.enqueue_message.

Channel-agnostic: the physical queue (QQ _message_queue / WS queue) is injected
through the context's enqueue_message callback, so this stage never touches a
WS-specific method.
"""

from __future__ import annotations

from pathlib import Path

from bot.input_pipeline.context import BotInputContext
from bot.input_pipeline.stages.resolve_pool import RoutingMeta
from modex_agent.input_pipeline.envelope import CommandStatus, UserInputEnvelope
from modex_agent.input_pipeline.stage import Continue, InputStage, StageResult
from modex_agent.messaging.models import InputMessage


class EnqueueStage(InputStage):
    async def process(
        self, envelope: UserInputEnvelope, ctx: BotInputContext
    ) -> StageResult:
        if envelope.command_status == CommandStatus.HANDLED:
            return Continue(value=envelope)
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
        msg = InputMessage(
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
        ctx.enqueue_message(msg)
        return Continue(value=envelope)
