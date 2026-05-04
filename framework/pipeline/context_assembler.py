"""ContextAssembler — load context, recover checkpoints, build system prompt, run multi-agent builder.

从 AgentPipeline._assemble_context 提取为独立模块级函数。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..core.emitter import AgentResult
from ..core.types import InputMessage, MessageRole
from ..memory.history import (
    ListMessageHistory,
    MessageHistory,
    history_to_list,
)

if TYPE_CHECKING:
    from ..multi_agent import AgentDescriptor, MultiAgentContextBuilder
    from ..core.skills import SkillManager
    from ..core.tool_manager import ToolManager

logger = logging.getLogger(__name__)


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
    agent_descriptor: "AgentDescriptor | None" = None,
    tool_manager: "ToolManager | None" = None,
    skill_manager: "SkillManager | None" = None,
    context_builder: "MultiAgentContextBuilder | None" = None,
) -> Any:
    """Assemble context state: load context, recover checkpoints, write user message,
    build system prompt, and run multi-agent context builder.

    Returns: context_state
    """
    source_agent = input_metadata.get("source_agent")

    context_state = await ctx_mgr.load_with_metadata(
        session_id,
        metadata={"input_metadata": input_metadata},
    )

    # crash recovery with dedup
    recover_fn = getattr(ctx_mgr, "recover_checkpoint", None)
    if recover_fn is not None:
        recovered, was_recovered = await recover_fn(session_id)
        if was_recovered:
            context_state = await ctx_mgr.load_with_metadata(
                session_id,
                metadata={"input_metadata": input_metadata},
            )
    else:
        recovered = await ctx_mgr.load_checkpoint(session_id)
        if recovered:
            memory_system = getattr(ctx_mgr, "memory_system", None)
            if memory_system is not None:
                ctx = getattr(ctx_mgr, "_context_cache", {}).get(session_id)
                if ctx is None:
                    from ..memory.core.scope import MemoryContext

                    ctx = MemoryContext(
                        session_id=session_id,
                        user_id=getattr(ctx_mgr, "default_user_id", "default"),
                    )
                await memory_system.add_messages(ctx, recovered)
            await ctx_mgr.clear_checkpoint(session_id)
            context_state = await ctx_mgr.load_with_metadata(
                session_id,
                metadata={"input_metadata": input_metadata},
            )

    # Build multimodal content
    if media_blocks and _media_processor is not None:
        try:
            multimodal_content = _media_processor.build_content(sanitized_content, media_blocks)
        except Exception:
            multimodal_content = sanitized_content
    else:
        multimodal_content = sanitized_content

    if source_agent:
        user_message: dict[str, Any] = {
            "role": MessageRole.AGENT,
            "source_agent": source_agent,
            "content": multimodal_content,
        }
    else:
        user_message = {"role": MessageRole.USER, "content": multimodal_content}

    context_state = await ctx_mgr.load(session_id)

    if not _is_approval_cmd:
        await context_state.history.append(user_message)
    await ctx_mgr.save(
        session_id=session_id,
        user_message=None,
        assistant_result=AgentResult(),
        metadata={"input_metadata": input_metadata},
    )

    # Restore full multimodal content in history
    if media_blocks and _media_processor is not None:
        from ..memory.history import restore_multimodal_in_history

        pending = await restore_multimodal_in_history(
            context_state.history, multimodal_content, logger
        )
        if pending is not None:
            context_state.history = ListMessageHistory(pending)

    # Build system prompt
    agent_name = agent_descriptor.address.name if agent_descriptor else "main"
    runtime_info: dict[str, Any] = {"caller_context": {"agent_name": agent_name}}
    if input_metadata:
        for key in ("user_id", "tenant_id", "channel", "chat_id"):
            if key in input_metadata:
                runtime_info[key] = input_metadata[key]
    context_state.system_prompt = await ctx_mgr.build_system_prompt(
        tool_manager=tool_manager,
        skill_manager=skill_manager,
        runtime_info=runtime_info,
    )

    # MultiAgentContextBuilder
    if context_builder is not None and agent_descriptor is not None:
        from ..multi_agent.address import AgentAddress
        from ..multi_agent.envelope import AgentMessageEnvelope

        envelope = AgentMessageEnvelope(
            payload={"content": multimodal_content},
            source=AgentAddress(kind="user", name=input_msg.sender_id or "unknown"),
            target=AgentAddress(
                kind="agent", name=route_result.agent_name if route_result else "main"
            ),
            message_type=(
                route_result.envelope_metadata.get("message_type", "agent_message")
                if route_result else "agent_message"
            ),
            conversation_id=route_result.conversation_id if route_result else session_id,
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
        non_system = [m for m in built_messages if m.get("role") != "system"]
        if user_message.get("role") == MessageRole.USER and not any(
            m.get("role") == MessageRole.USER for m in non_system
        ):
            non_system = list(non_system) + [user_message]
        if isinstance(context_state.history, MessageHistory) and not isinstance(
            context_state.history, ListMessageHistory
        ):
            await context_state.history.replace_all(non_system)
        else:
            context_state.history = ListMessageHistory(non_system)

    return context_state
