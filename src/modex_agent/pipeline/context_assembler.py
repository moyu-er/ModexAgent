"""ContextAssembler — load context, build system prompt, run multi-agent builder.

从 AgentPipeline._assemble_context 提取为独立模块级函数。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from modex_agent.core.emitter import AgentResult
from modex_agent.core.types import InputMessage, MessageRole
from modex_agent.memory.history import (
    ListMessageHistory,
    history_to_list,
)
from modex_agent.multi_agent.message_type import AgentMessageType

if TYPE_CHECKING:
    from modex_agent.core.skills import SkillManager
    from modex_agent.core.tool_manager import ToolManager
    from modex_agent.multi_agent import AgentDescriptor
    from modex_agent.utils.context_builder import MultiAgentContextBuilder


async def assemble_context(
    session_id: str,
    input_msg: InputMessage,
    input_metadata: dict[str, Any],
    sanitized_content: str | None,
    media_blocks: list[Any],
    _media_processor: Any | None,
    ctx_mgr: Any,
    route_result: Any | None,
    _is_approval_cmd: bool,
    *,
    agent_descriptor: AgentDescriptor | None = None,
    tool_manager: ToolManager | None = None,
    skill_manager: SkillManager | None = None,
    context_builder: MultiAgentContextBuilder | None = None,
    append_user_message: bool = True,
) -> Any:
    """Assemble context state: load context, write user message,
    and run multi-agent context builder.

    Returns: context_state
    """
    source_agent = input_metadata.get("source_agent")

    # Build multimodal content.
    # NOTE: mechanism A (native multimodal) is implemented via turn-state
    # enrichment in ``LLMNode._build_messages`` (ADR-0014 §2/§7), NOT here.
    # This branch is the dormant MediaProcessor seam retained for the deferred
    # provider-side renderer (ADR-0013 §10) and is currently unreachable —
    # ``preprocess`` always returns ``[]``/``None`` for ``media_blocks``.
    if media_blocks and _media_processor is not None:
        try:
            multimodal_content = _media_processor.build_content(sanitized_content, media_blocks)
        except Exception:
            multimodal_content = sanitized_content
    else:
        multimodal_content = sanitized_content

    if source_agent:
        user_message = {
            "role": MessageRole.AGENT,
            "source_agent": source_agent,
            "content": multimodal_content,
        }
    else:
        user_message = {"role": MessageRole.USER, "content": multimodal_content}

    # Propagate content_format / truncatable_paths from input message
    # so governance can protect XML structure (agent messages, etc.)
    if input_msg.content_format is not None:
        user_message["content_format"] = input_msg.content_format
    if input_msg.truncatable_paths is not None:
        user_message["truncatable_paths"] = input_msg.truncatable_paths

    agent_name = agent_descriptor.address.name if agent_descriptor else "main"
    runtime_info: dict[str, Any] = {"caller_context": {"agent_name": agent_name}}
    if input_metadata:
        for key in ("user_id", "tenant_id", "channel", "chat_id"):
            if key in input_metadata:
                runtime_info[key] = input_metadata[key]
    # Thread the authoritative parent link (stamped on the session by
    # dispatch_envelope from the envelope) into the prompt providers, so
    # APPEND/FORK read it from the turn instead of recovering it from a
    # workspace-partitioned session store.
    parent_sid = input_msg.session.parent_session_id if input_msg.session else None
    if parent_sid:
        runtime_info["parent_session_id"] = parent_sid
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
        from modex_agent.multi_agent.address import AgentAddress
        from modex_agent.multi_agent.envelope import AgentMessageEnvelope

        envelope = AgentMessageEnvelope(
            payload={"content": multimodal_content},
            source=AgentAddress(kind="user", name=input_msg.sender_id or "unknown"),
            target=AgentAddress(
                kind="agent", name=route_result.session.agent_name if route_result else "main"
            ),
            message_type=(
                route_result.envelope_metadata.get(
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
