"""ContextAssembler — load context, build system prompt, run multi-agent builder.

从 AgentPipeline._assemble_context 提取为独立模块级函数。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modex_agent.core.constants import RuntimeInfoKey
from modex_agent.core.context import ContextManager, ContextState
from modex_agent.core.emitter import AgentResult
from modex_agent.core.media import Kind
from modex_agent.core.message import (
    ContentPart,
    ImageUrl,
    ImageUrlPart,
    TextPart,
    build_media_ref,
)
from modex_agent.core.types import InputMessage, MessageRole, ReminderKind
from modex_agent.memory.history import (
    ListMessageHistory,
    history_to_list,
)
from modex_agent.multi_agent.message_format import build_agent_reminder_record
from modex_agent.multi_agent.message_type import AgentMessageType

if TYPE_CHECKING:
    from modex_agent.core.capabilities import ModelInfo
    from modex_agent.core.skills import SkillManager
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.multi_agent.router import RouteResult
    from modex_agent.utils.context_builder import MultiAgentContextBuilder


async def assemble_context(
    session_id: str,
    input_msg: InputMessage,
    input_metadata: dict[str, Any],
    sanitized_content: str | None,
    ctx_mgr: ContextManager,
    route_result: RouteResult | None,
    _is_approval_cmd: bool,
    *,
    agent_descriptor: AgentDescriptor | None = None,
    tool_manager: ToolManager | None = None,
    skill_manager: SkillManager | None = None,
    context_builder: MultiAgentContextBuilder | None = None,
    append_user_message: bool = True,
    model_info: ModelInfo | None = None,
) -> ContextState:
    """Assemble context state: load context, write user message,
    and run multi-agent context builder.

    Returns: context_state
    """
    source_agent = input_metadata.get("source_agent")

    # User-message parts carrier: image-kind resolved attachments lower to
    # [TextPart(sanitized text, 含机制B引用行), ImageUrlPart(media://<aid>), ...]
    # UNCONDITIONALLY — the carrier is model-agnostic. The media:// references
    # (never base64) are what persist into history; inject_multimodal applies
    # the per-request modality gate + budget + resolution at each LLM call, so
    # a text-only turn degrades to the text + mechanism-B lines and a later
    # vision turn still sees the images.
    image_attachments = [
        a for a in input_msg.attachments_resolved if a.kind is Kind.IMAGE
    ]
    multimodal_content: str | list[ContentPart] | None
    if image_attachments:
        parts: list[ContentPart] = [TextPart(text=sanitized_content or "")]
        parts.extend(
            ImageUrlPart(image_url=ImageUrl(url=build_media_ref(att.id)))
            for att in image_attachments
        )
        multimodal_content = parts
    else:
        multimodal_content = sanitized_content

    if source_agent:
        reminder_kind_raw = input_metadata.get("reminder_kind")
        reminder_kind = ReminderKind(reminder_kind_raw) if reminder_kind_raw else None
        message_type_raw = input_metadata.get("message_type")
        message_type = AgentMessageType(message_type_raw) if message_type_raw else None
        invocation_id_raw = input_metadata.get("invocation_id")
        invocation_id = str(invocation_id_raw) if invocation_id_raw else None
        agent_content = (
            multimodal_content if isinstance(multimodal_content, str) else sanitized_content
        )
        user_message = build_agent_reminder_record(
            agent_content,
            source_agent=str(source_agent),
            reminder_kind=reminder_kind,
            message_type=message_type,
            invocation_id=invocation_id,
        )
    else:
        user_message = {"role": MessageRole.USER, "content": multimodal_content}
        # Propagate skill-command XML structure only for non-agent input.
        if input_msg.content_format is not None:
            user_message["content_format"] = input_msg.content_format
        if input_msg.truncatable_paths is not None:
            user_message["truncatable_paths"] = input_msg.truncatable_paths

    agent_name = agent_descriptor.address.name if agent_descriptor else "main"

    runtime_info: dict[str, Any] = {RuntimeInfoKey.CALLER_CONTEXT: {"agent_name": agent_name}}
    if input_metadata:
        for key in (
            RuntimeInfoKey.USER_ID,
            RuntimeInfoKey.TENANT_ID,
            RuntimeInfoKey.CHANNEL,
            RuntimeInfoKey.CHAT_ID,
        ):
            if key in input_metadata:
                runtime_info[key] = input_metadata[key]
    parent_sid = input_msg.session.parent_session_id if input_msg.session else None
    if parent_sid:
        runtime_info[RuntimeInfoKey.PARENT_SESSION_ID] = parent_sid
    if model_info is not None:
        runtime_info[RuntimeInfoKey.MODEL_INFO] = model_info
    if input_msg.workspace is not None:
        runtime_info[RuntimeInfoKey.WORKING_DIRECTORY] = input_msg.workspace
    context_state = await ctx_mgr.load(
        session_id,
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        runtime_info=runtime_info,
    )

    if append_user_message and not _is_approval_cmd:
        await context_state.history.append(user_message)
    await ctx_mgr.save(
        session_id=session_id,
        user_message=None,
        assistant_result=AgentResult(),
        metadata={"input_metadata": input_metadata},
    )

    sideband_prompt = input_metadata.get("sideband_system_prompt")
    if isinstance(sideband_prompt, str) and sideband_prompt:
        context_state.system_prompt = "\n\n".join(
            part for part in (context_state.system_prompt, sideband_prompt) if part
        )

    # MultiAgentContextBuilder
    if context_builder is not None and agent_descriptor is not None:
        from modex_agent.messaging.broker import AddressKind
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.envelope import AgentMessageEnvelope

        envelope = AgentMessageEnvelope(
            payload={"content": multimodal_content},
            source=AgentAddress(kind=AddressKind.USER, name=input_msg.sender_id or "unknown"),
            target=AgentAddress(
                kind=AddressKind.AGENT,
                name=route_result.session.agent_name if route_result else "main",
            ),
            message_type=(
                (route_result.envelope_metadata or {}).get(
                    "message_type", AgentMessageType.AGENT_MESSAGE
                )
                if route_result
                else AgentMessageType.AGENT_MESSAGE
            ),
            session_id=route_result.session.session_id_prefix if route_result else session_id,
            agent_session_id=session_id,
            metadata=input_metadata,
        )
        base_history = await history_to_list(context_state.history)
        if base_history and base_history[-1].get("role") == MessageRole.USER:
            base_history = base_history[:-1]
        built_messages = context_builder.build_messages(
            history=base_history,
            current_envelope=envelope,
            agent_descriptor=agent_descriptor,
        )
        system_msgs = [m for m in built_messages if m.get("role") == "system"]
        if system_msgs:
            context_state.system_prompt = "\n\n".join(m.get("content", "") for m in system_msgs)
            if isinstance(sideband_prompt, str) and sideband_prompt:
                context_state.system_prompt = "\n\n".join(
                    part for part in (context_state.system_prompt, sideband_prompt) if part
                )
        non_system = [m for m in built_messages if m.get("role") != "system"]
        if (
            append_user_message
            and user_message.get("role") == MessageRole.USER
            and not any(m.get("role") == MessageRole.USER for m in non_system)
        ):
            non_system = list(non_system) + [user_message]
        try:
            await context_state.history.replace_all(non_system)
        except (AttributeError, NotImplementedError):
            context_state.history = ListMessageHistory(non_system)

    return context_state
