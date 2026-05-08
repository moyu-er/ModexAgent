"""MemorySystem and adapter for ContextManager integration."""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from framework.core.context import ContextManager, ContextState
from framework.core.emitter import AgentResult
from framework.core.skills import ResolutionContext, SkillManager
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import MemoryAgentRole, MemoryContext
from framework.memory.core.system import (
    ContextManagedMemorySystem,
    MemorySystem,  # noqa: F401 — re-export
)
from framework.memory.default_system import DefaultMemorySystem
from framework.memory.layers.config import MemoryLayerConfigSet
from framework.memory.layers.factory import MemoryLayerFactory
from framework.memory.lifecycle import MemoryLifecyclePolicy
from framework.memory.registry.file import DefaultMemoryStoreRegistry

logger = logging.getLogger(__name__)

_ERROR_PLACEHOLDER = "[Assistant reply unavailable due to model or runtime error.]"


def create_memory_system(
    workspace: Path,
    config: MemoryLayerConfigSet | None = None,
    llm_provider: Any | None = None,
    session_only: bool = False,
    lifecycle_policy: MemoryLifecyclePolicy | None = None,
) -> DefaultMemorySystem:
    """Create a production-ready memory system with default local-file registry.

    Args:
        workspace: Root directory for file-based storage.
        config: Optional layer configuration set.
        llm_provider: Optional LLM provider for compression/summarization.
        session_only: If True, create session-only layers (peer/subagent — no archive, no knowledge).
    """
    registry = DefaultMemoryStoreRegistry(workspace)
    if session_only:
        session_config = config.session if config else None
        pending_config = config.pending if config else None
        layer_set = MemoryLayerFactory.session_only(
            registry=registry,
            config=session_config,
            pending_config=pending_config,
        )
    else:
        layer_set = MemoryLayerFactory.single_user(
            registry=registry,
            config=config,
            llm_provider=llm_provider,
        )
    return DefaultMemorySystem(
        layer_set=layer_set,
        store_registry=registry,
        lifecycle_policy=lifecycle_policy,
    )


class MemorySystemContextManager(ContextManager):
    """Adapter that wraps a ``MemorySystem`` behind the ``ContextManager`` interface.

    This is a bridge so the existing pipeline can consume the new memory
    system without being rewritten.  New code should depend on
    ``MemorySystem`` directly.
    """

    def __init__(
        self,
        memory_system: ContextManagedMemorySystem,
        default_user_id: str = "default",
        default_agent_id: str | None = None,
        default_agent_role: str | MemoryAgentRole | None = None,
        base_system_prompt: str = "",
        injection_policy: Any | None = None,
    ):
        from framework.memory.injection import FullInjectionPolicy

        self.memory_system: ContextManagedMemorySystem = memory_system
        self.default_user_id = default_user_id
        self.default_agent_id = default_agent_id
        self.default_agent_role = default_agent_role
        self.base_system_prompt = base_system_prompt
        self.injection_policy: Any = (
            injection_policy or FullInjectionPolicy()
        )
        self._last_session_id: str | None = None
        self._context_cache: dict[str, MemoryContext] = {}
        self._max_context_cache_size = 1000

    # -- ContextManager interface -----------------------------------------

    async def load_with_metadata(
        self, session_id: str, metadata: dict[str, Any] | None = None
    ) -> ContextState:
        return await self.load(session_id, metadata=metadata)

    async def load(
        self,
        session_id: str,
        runtime_info: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ContextState:
        self._last_session_id = session_id
        ctx: MemoryContext
        if runtime_info or metadata:
            ctx = self._build_context(session_id, runtime_info=runtime_info, metadata=metadata)
        else:
            cached_ctx = self._context_cache.get(session_id)
            if cached_ctx is None:
                ctx = MemoryContext(
                    session_id=session_id,
                    user_id=self.default_user_id,
                    agent_id=self.default_agent_id,
                    agent_role=self.default_agent_role,
                )
            else:
                ctx = cached_ctx
        # Budget enforcement before every LLM request
        try:
            await self.memory_system.ensure_within_budget(ctx)
        except Exception:
            logger.warning("Pre-load budget check failed", exc_info=True)

        # Extract query from runtime_info for provider prefetch
        query = ""
        if runtime_info and "message" in runtime_info:
            query = str(runtime_info["message"])

        bundle = await self.injection_policy.assemble(
            context=ctx,
            memory_system=self.memory_system,
            query=query,
        )
        return self._bundle_to_state(bundle, ctx)

    async def save(
        self,
        session_id: str,
        user_message: ChatMessage | dict[str, Any] | None,
        assistant_result: AgentResult,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        ctx = self._build_context(session_id, metadata=metadata)
        input_metadata = metadata.get("input_metadata") if metadata else None
        if user_message:
            prefixed_message = self._apply_runtime_context_prefix(user_message, input_metadata)
            await self.memory_system.add_messages(ctx, [prefixed_message])
            # Mark pending user turn for crash recovery
            try:
                set_pending = getattr(self.memory_system, "set_pending_user_turn", None)
                if set_pending is not None:
                    msg_id = metadata.get("message_id", "") if metadata else ""
                    await set_pending(ctx, msg_id, time.time())
            except Exception:
                logger.debug("Failed to set pending user turn for %s", session_id, exc_info=True)

        # Clear pending user turn since assistant completed (success or error placeholder handled separately)
        if assistant_result.stop_reason != "error" or assistant_result.error:
            try:
                clear_pending = getattr(self.memory_system, "clear_pending_user_turn", None)
                if clear_pending is not None:
                    await clear_pending(ctx)
            except Exception:
                logger.debug("Failed to clear pending user turn for %s", session_id, exc_info=True)

    async def flush(self, session_id: str) -> None:
        pass  # All messages written in real-time through ScopedMessageHistory

    async def save_checkpoint(
        self, session_id: str, messages: Sequence[ChatMessage | dict[str, Any]]
    ) -> None:
        ctx = self._context_cache.get(session_id)
        if ctx is None:
            ctx = MemoryContext(
                session_id=session_id,
                user_id=self.default_user_id,
                agent_id=self.default_agent_id,
                agent_role=self.default_agent_role,
            )
        await self.memory_system.save_checkpoint(ctx, messages)

    async def load_checkpoint(self, session_id: str) -> list[ChatMessage] | None:
        ctx = self._context_cache.get(session_id)
        if ctx is None:
            ctx = MemoryContext(
                session_id=session_id,
                user_id=self.default_user_id,
                agent_id=self.default_agent_id,
                agent_role=self.default_agent_role,
            )
        return await self.memory_system.load_checkpoint(ctx)

    async def clear_checkpoint(self, session_id: str) -> None:
        ctx = self._context_cache.get(session_id)
        if ctx is None:
            ctx = MemoryContext(
                session_id=session_id,
                user_id=self.default_user_id,
                agent_id=self.default_agent_id,
                agent_role=self.default_agent_role,
            )
        await self.memory_system.clear_checkpoint(ctx)

    async def recover_checkpoint(
        self, session_id: str
    ) -> tuple[list[ChatMessage] | None, bool]:
        """Recover messages from checkpoint with deduplication (14.2).

        Returns (messages, was_recovered):
        - messages: the recovered messages, or None if no checkpoint / already recovered
        - was_recovered: True if recovery was performed, False if skipped (dedup)
        """
        ctx = self._build_context(session_id)
        checkpoint_id = await self.memory_system.get_checkpoint_id(ctx)
        if checkpoint_id is None:
            return None, False

        last_recovered = await self.memory_system.get_last_recovered_checkpoint_id(ctx)
        if last_recovered == checkpoint_id:
            logger.debug(
                "Checkpoint %s already recovered for %s, skipping dedup",
                checkpoint_id,
                session_id,
            )
            return None, False

        recovered = await self.memory_system.load_checkpoint(ctx)
        if recovered is None:
            return None, False

        # Overlap dedup: compare recovered messages with history tail
        history = await self.memory_system.get_history(ctx)
        if history and len(history) >= len(recovered):
            # Check if the last N messages match the recovered ones
            tail = history[-len(recovered):]
            overlap = all(
                h.role == r.role and h.content == r.content
                for h, r in zip(tail, recovered, strict=False)
            )
            if overlap:
                logger.debug(
                    "Checkpoint messages already present in history for %s, skipping",
                    session_id,
                )
                await self.memory_system.set_last_recovered_checkpoint_id(
                    ctx, checkpoint_id
                )
                await self.memory_system.clear_checkpoint(ctx)
                return None, False

        await self.memory_system.add_messages(ctx, recovered)
        await self.memory_system.set_last_recovered_checkpoint_id(ctx, checkpoint_id)
        await self.memory_system.clear_checkpoint(ctx)
        logger.info(
            "Recovered %d messages from checkpoint for %s",
            len(recovered),
            session_id,
        )
        return recovered, True

    async def add_assistant_placeholder(self, session_id: str, error: str) -> None:
        """Write an assistant error placeholder message to history.

        Only writes when the last message in history is a user message,
        preventing duplicate placeholders. Detailed error goes to metadata.
        Also clears the pending user turn marker.
        """
        ctx = self._build_context(session_id)
        try:
            history = await self.memory_system.get_history(ctx, max_messages=1)
        except Exception:
            logger.warning("Failed to get history for placeholder check", exc_info=True)
            return

        if history and history[-1].role == "user":
            placeholder_dict: dict[str, Any] = {
                "role": "assistant",
                "content": _ERROR_PLACEHOLDER,
                "metadata": {"error": error, "is_error_placeholder": True},
            }
            placeholder = ChatMessage.coerce(placeholder_dict)
            try:
                await self.memory_system.add_messages(ctx, [placeholder])
            except Exception:
                logger.exception("Failed to write error placeholder for %s", session_id)

        # Clear pending user turn regardless of whether we wrote the placeholder
        try:
            clear_pending = getattr(self.memory_system, "clear_pending_user_turn", None)
            if clear_pending is not None:
                await clear_pending(ctx)
        except Exception:
            logger.debug("Failed to clear pending user turn for %s", session_id, exc_info=True)

    async def clear(self, session_id: str) -> None:
        ctx = self._context_cache.get(session_id)
        if ctx is None:
            ctx = MemoryContext(
                session_id=session_id,
                user_id=self.default_user_id,
                agent_id=self.default_agent_id,
                agent_role=self.default_agent_role,
            )
        await self.memory_system.clear(ctx)

    # -- System prompt composition ----------------------------------------

    async def build_system_prompt(
        self,
        tool_manager: Any,
        skill_manager: SkillManager | None = None,
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        session_id = (
            runtime_info.get("session_id")
            if runtime_info and "session_id" in runtime_info
            else self._last_session_id
        ) or "default"

        ctx = self._build_context(session_id, runtime_info=runtime_info)
        query = ""
        if runtime_info and "message" in runtime_info:
            query = str(runtime_info["message"])

        bundle = await self.injection_policy.assemble(
            context=ctx, memory_system=self.memory_system, query=query,
        )

        parts: list[str] = []
        if self.base_system_prompt:
            parts.append(self.base_system_prompt)
        for section in (bundle.system_sections or []):
            parts.append(section.content)

        if skill_manager is not None:
            skill_prompt = await skill_manager.build_prompt(
                ResolutionContext.from_runtime(tool_manager=tool_manager)
            )
            if skill_prompt:
                parts.append(skill_prompt)
        if runtime_info:
            runtime_text = self._format_runtime_info(runtime_info)
            if runtime_text:
                parts.append(runtime_text)

        return "\n\n---\n\n".join(parts)

    def get_active_contexts(self) -> list[MemoryContext]:
        return list(self._context_cache.values())

    # -- Internal helpers -------------------------------------------------

    def _bundle_to_state(self, bundle: Any, ctx: MemoryContext) -> ContextState:
        """Convert a MemoryContextBundle to ContextState, injecting base prompt."""
        parts: list[str] = []
        if self.base_system_prompt:
            parts.append(self.base_system_prompt)
        for section in (bundle.system_sections or []):
            parts.append(section.content)
        system_prompt = "\n\n---\n\n".join(parts) if parts else ""

        history = self.memory_system.create_message_history(
            context=ctx, initial_messages=bundle.messages,
        )
        return ContextState(system_prompt=system_prompt, history=history)

    def _build_context(
        self,
        session_id: str,
        runtime_info: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryContext:
        input_metadata = metadata.get("input_metadata") if metadata else None

        def _extract(key: str) -> str | None:
            if runtime_info and key in runtime_info:
                value = runtime_info.get(key)
                return str(value) if value is not None else None
            if input_metadata and key in input_metadata:
                value = input_metadata.get(key)
                return str(value) if value is not None else None
            return None

        user_id = _extract("user_id") or self.default_user_id
        agent_id = _extract("agent_id") or self.default_agent_id
        agent_role = _extract("agent_role") or self.default_agent_role
        tenant_id = _extract("tenant_id")
        channel = _extract("channel")
        chat_id = _extract("chat_id")
        sender_agent = _extract("sender_agent") or _extract("source_agent")
        receiver_agent = _extract("receiver_agent")

        if session_id and ":" in session_id:
            parts = session_id.split(":")
            if len(parts) == 3:
                if sender_agent is None:
                    sender_agent = parts[1]
                if receiver_agent is None:
                    receiver_agent = parts[2]

        ctx = MemoryContext(
            session_id=session_id,
            user_id=user_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            agent_role=agent_role,
            channel=channel,
            chat_id=chat_id,
            sender_agent=sender_agent,
            receiver_agent=receiver_agent,
        )
        if len(self._context_cache) >= self._max_context_cache_size:
            self._context_cache.clear()
        self._context_cache[session_id] = ctx
        return ctx

    def _apply_runtime_context_prefix(
        self,
        user_message: ChatMessage | dict[str, Any],
        input_metadata: dict[str, Any] | None = None,
    ) -> ChatMessage | dict[str, Any]:
        if not input_metadata:
            return user_message
        runtime_lines: list[str] = []
        if "channel" in input_metadata:
            runtime_lines.append(f"channel={input_metadata['channel']}")
        if "chat_id" in input_metadata:
            runtime_lines.append(f"chat_id={input_metadata['chat_id']}")
        if not runtime_lines:
            return user_message

        msg_dict = (
            user_message.to_dict() if isinstance(user_message, ChatMessage) else dict(user_message)
        )
        original_content = msg_dict.get("content", "")
        prefix = "[Runtime Context]\n" + "\n".join(runtime_lines) + "\n\n"

        if isinstance(original_content, list):
            if not original_content:
                return user_message
            new_content: str | list[dict[str, Any]] = [
                {"type": "text", "text": prefix},
                *list(original_content),
            ]
        else:
            new_content = prefix + str(original_content)

        msg_dict["content"] = new_content
        if isinstance(user_message, ChatMessage):
            return ChatMessage.coerce(msg_dict)
        return msg_dict

    @staticmethod
    def _format_runtime_info(info: dict[str, Any]) -> str:
        lines = ["## Runtime"]
        if "current_time" in info:
            lines.append(f"Current Time: {info['current_time']}")
        if "platform" in info:
            lines.append(f"Platform: {info['platform']}")
        return "\n".join(lines)
