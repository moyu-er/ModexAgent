"""MemorySystem and adapter for ContextManager integration."""

from __future__ import annotations

from modex_agent.core.prompt import SystemPromptProvider

# Default system prompt used when no other content is configured.
# Kept minimal so the agent is useful even without custom configuration.
_DEFAULT_SYSTEM_PROMPT = (
    "You are an AI assistant.\n\n"
    "## Interaction Guidelines\n"
    "- Respond naturally and concisely.\n"
    "- Give direct answers first, then add explanation if needed.\n"
    "- If the user's intent is unclear, ask for clarification before guessing.\n"
    "- Be honest about uncertainty — never fabricate information.\n"
    "- Use code blocks for code and commands."
)

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.core.context import ContextManager, ContextState
from modex_agent.core.emitter import AgentResult
from modex_agent.core.skills import SkillManager
from modex_agent.memory.context_governance import ContextGovernance
from modex_agent.core.message import ChatMessage
from modex_agent.core.scope import MemoryAgentRole, MemoryContext
from modex_agent.memory.core.system import (
    MemorySystem,  # noqa: F401 — re-export
)
from modex_agent.memory.default_system import DefaultMemorySystem
from modex_agent.memory.layers.config import MemoryLayerConfigSet
from modex_agent.memory.layers.factory import MemoryLayerFactory
from modex_agent.memory.pruned.manager import PrunedManager

# UserRetentionBuffer injection moved to framework.memory.user_buffer (Task 6 stub)
from modex_agent.memory.registry.file import DefaultMemoryStoreRegistry

if TYPE_CHECKING:
    from modex_agent.agents.summarizer.abc import ArchiveGenerator, KnowledgeConsolidatorBase
    from modex_agent.core.experience import ExperienceManager
    from modex_agent.core.provider import LLMProvider
    from modex_agent.memory.stores.dir_archive import DirArchiveStorage

logger = logging.getLogger(__name__)


def create_memory_system(
    workspace: Path,
    config: MemoryLayerConfigSet | None = None,
    llm_provider: LLMProvider | None = None,
    session_only: bool = False,
    cleanup_config: dict[str, int | float] | None = None,
    pruned_manager: PrunedManager | None = None,
    archive_agent: ArchiveGenerator | None = None,
    archive_storage: DirArchiveStorage | None = None,
    knowledge_consolidator: KnowledgeConsolidatorBase | None = None,
    archive_trigger_callback: Callable[[MemoryContext], Awaitable[None]] | None = None,
) -> DefaultMemorySystem:
    """Create a production-ready memory system with default local-file registry.

    Args:
        workspace: Root directory for file-based storage.
        config: Optional layer configuration set.
        llm_provider: Optional LLM provider for compression/summarization.
        session_only: If True, create session-only layers (subagent — no archive, no knowledge).
    """
    registry = DefaultMemoryStoreRegistry(workspace)
    if session_only:
        session_config = config.session if config else None
        user_retention_config = config.user_retention if config else None
        layer_set = MemoryLayerFactory.session_only(
            registry=registry,
            config=session_config,
            user_retention_config=user_retention_config,
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
        cleanup_config=cleanup_config,
        pruned_manager=pruned_manager,
        archive_agent=archive_agent,
        archive_storage=archive_storage,
        knowledge_consolidator=knowledge_consolidator,
        archive_trigger_callback=archive_trigger_callback,
    )


class MemorySystemContextManager(ContextManager):
    """Adapter that wraps a ``MemorySystem`` behind the ``ContextManager`` interface.

    Prompt assembly order (in :meth:`load`):
      1. Runtime metadata (date, platform)
      2. Base system prompt (agent personality / system.md)
      3. Memory layers via ``injection_policy.assemble()`` — session, archive,
         knowledge, user-retention (subject to budget & pruning)
      4. Experiences — persistent reference knowledge (NOT a memory layer)
      5. Skills — persistent reference knowledge (NOT a memory layer)

    Skills and experiences are intentionally kept OUTSIDE the memory
    injection pipeline because they are static reference content — they
    do not participate in memory lifecycle (no budget enforcement, no
    truncation, no eviction at the injection level).  Their own managers
    handle freshness / validity independently.
    """

    def __init__(
        self,
        memory_system: DefaultMemorySystem,
        default_user_id: str = "default",
        default_agent_id: str | None = None,
        default_agent_role: str | MemoryAgentRole | None = None,
        base_system_prompt: str = "",
        injection_policy: Any | None = None,
        experience_manager: ExperienceManager | None = None,
        output_base_dir: Path | None = None,
    ) -> None:
        from modex_agent.memory.injection import FullInjectionPolicy

        self.memory_system: DefaultMemorySystem = memory_system
        self.default_user_id = default_user_id
        self.default_agent_id = default_agent_id
        self.default_agent_role = default_agent_role
        self.base_system_prompt = base_system_prompt
        self.injection_policy: Any = injection_policy or FullInjectionPolicy()
        self._last_session_id: str | None = None
        self._context_cache: dict[str, MemoryContext] = {}
        self._max_context_cache_size = 1000
        self._experience_manager = experience_manager
        self._output_base_dir: Path | None = output_base_dir

    def wrap_governance(
        self,
        governance: ContextGovernance | None,
        session_id: str,
    ) -> ContextGovernance | None:
        try:
            urb = self.memory_system.layers.user_retention
        except AttributeError:
            return governance
        if urb is None:
            return governance
        from modex_agent.memory.context_governance import (
            CompositeGovernance,
            UserRetentionBufferInjectionGovernance,
        )
        from modex_agent.memory.layers.config import UserRetentionBufferConfig

        # Mirror the URB layer's default entry limit (3) for injection.
        injector = UserRetentionBufferInjectionGovernance(
            urb=urb,
            context_factory=lambda: (
                self._context_cache.get(session_id)
                or MemoryContext(
                    session_id=session_id,
                    user_id=self.default_user_id,
                    agent_id=self.default_agent_id,
                    agent_role=self.default_agent_role,
                )
            ),
            max_entries=UserRetentionBufferConfig().max_entries,
        )
        if governance is not None:
            return CompositeGovernance([governance, injector])
        return injector

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
        tool_manager: Any = None,
        skill_manager: SkillManager | None = None,
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

        # ── Prompt assembly ────────────────────────────────────────────
        # Build SystemPromptPipeline with individual providers.
        # Archive and Pruned have dedicated refreshable providers.
        # The injection_policy provides: disclaimer + knowledge + blocks + prefetch.
        # ────────────────────────────────────────────────────────────────
        from modex_agent.memory.injection.full_injection import FullInjectionPolicy
        from modex_agent.core.prompt import SystemPromptPipeline
        from modex_agent.memory.prompt_pipeline.providers import (
            ArchiveProvider,
            BasePromptProvider,
            ExperienceProvider,
            KnowledgeProvider,
            ProviderBlocksProvider,
            ProviderPrefetchProvider,
            PrunedProvider,
            RuntimeProvider,
            SkillProvider,
        )

        # If the policy injects archive/pruned content itself, those sections are
        # handled by dedicated refreshable providers below; use a clean policy
        # that skips them to avoid double emission.
        policy = self.injection_policy
        if policy.injects_pruned() or policy.injects_archive():
            pipeline_policy = FullInjectionPolicy(pruned_manager=None, archive_inject_count=0)
        else:
            pipeline_policy = policy
        result = await pipeline_policy.assemble(
            context=ctx,
            memory_system=self.memory_system,
            query=query,
        )

        providers: list[SystemPromptProvider] = []

        # 1. Runtime metadata (refreshes daily)
        if runtime_info:
            providers.append(RuntimeProvider())

        # 2. Base system prompt (static)
        if self.base_system_prompt:
            providers.append(BasePromptProvider(self.base_system_prompt))

        # 2b. OUTPUT.md path — dynamic per-session (subagents only)
        if self._output_base_dir is not None:
            from modex_agent.memory.prompt_pipeline.providers import OutputMdProvider

            providers.append(OutputMdProvider(self._output_base_dir, session_id))

        # 3. Memory layers from injection policy (disclaimer + knowledge + blocks + prefetch)
        if result.system_prompt:
            providers.append(KnowledgeProvider(result.system_prompt))

        # 4. Archive summaries (must refresh on cleanup)
        archive_dir = None
        try:
            archive_dir = await self.memory_system.get_storage_path(ctx)
        except Exception:
            logger.debug("Failed to resolve archive directory", exc_info=True)
        if archive_dir is not None:
            providers.append(ArchiveProvider(archive_dir))

        # 5. Pruned catalog (must refresh on cleanup)
        pruned_mgr = self.memory_system.pruned_manager
        if pruned_mgr is not None:
            providers.append(PrunedProvider(pruned_mgr, session_id=session_id))

        # 6. Provider blocks (hash-based versioning)
        provider_blocks: list[str] = []
        for prov in self.memory_system.get_providers():
            try:
                block = prov.system_prompt_block()
                if block:
                    provider_blocks.append(block)
            except Exception:
                continue
        if provider_blocks:
            providers.append(ProviderBlocksProvider(provider_blocks))

        # 7. Provider prefetch (query-based versioning)
        if query:
            try:
                prefetch = await self.memory_system.prefetch_memories(query, ctx)
                if prefetch:
                    providers.append(ProviderPrefetchProvider(query, prefetch))
            except Exception:
                pass

        # 8. Experience (scope-aware via context)
        if self._experience_manager is not None:
            try:
                experience_prompt = await self._experience_manager.build_prompt(
                    context=ctx,
                )
            except Exception:
                # Elevated from debug: a single malformed experience used to
                # silently drop ALL experiences from the system prompt.
                logger.warning("Failed to build experience prompt", exc_info=True)
            else:
                if experience_prompt:
                    providers.append(ExperienceProvider(experience_prompt))

        # 9. Skills (static)
        if skill_manager is not None:
            from modex_agent.core.skills import ResolutionContext

            skill_prompt = await skill_manager.build_prompt(
                ResolutionContext.from_runtime(tool_manager=tool_manager)
            )
            if skill_prompt:
                providers.append(SkillProvider(skill_prompt))

        pipeline = SystemPromptPipeline(providers)

        # Build a static fallback system_prompt for backward compatibility.
        # When all providers are empty, use the default prompt so the agent
        # remains functional even without custom configuration.
        static_parts: list[str] = []
        if runtime_info:
            runtime_text = self._format_runtime_info(runtime_info)
            if runtime_text:
                static_parts.append(runtime_text)
        if self.base_system_prompt:
            static_parts.append(self.base_system_prompt)
        if result.system_prompt:
            static_parts.append(result.system_prompt)

        # Assemble fallback string
        system_prompt = "\n\n---\n\n".join(static_parts) if static_parts else ""

        # If absolutely nothing is configured, inject the default prompt
        if not system_prompt:
            system_prompt = _DEFAULT_SYSTEM_PROMPT

        history = self.memory_system.create_message_history(
            context=ctx,
            initial_messages=result.messages,
        )
        return ContextState(
            system_prompt=system_prompt,
            history=history,
            system_prompt_pipeline=pipeline,
        )

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

    async def flush(self, session_id: str) -> None:
        pass  # All messages written in real-time through ScopedMessageHistory

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
        """Build system prompt by delegating to load() and resolving pipeline."""
        state = await self.load(
            session_id=self._last_session_id or "default",
            tool_manager=tool_manager,
            skill_manager=skill_manager,
            runtime_info=runtime_info,
        )
        if state.system_prompt_pipeline is not None:
            return await state.system_prompt_pipeline.get_or_refresh()
        return state.system_prompt

    def get_active_contexts(self) -> list[MemoryContext]:
        return list(self._context_cache.values())

    # -- Internal helpers -------------------------------------------------

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

        try:
            msg_dict = user_message.to_dict()
        except AttributeError:
            msg_dict = dict(user_message)
        original_content = msg_dict.get("content", "")
        prefix = "[Runtime Context]\n" + "\n".join(runtime_lines) + "\n\n"

        try:
            # Detect list-like content (multimodal) vs string.
            # Strings don't support + [] — they raise TypeError.
            _ = original_content + []
            if not original_content:
                return user_message
            new_content: str | list[dict[str, Any]] = [
                {"type": "text", "text": prefix},
                *list(original_content),
            ]
        except TypeError:
            new_content = prefix + str(original_content)

        msg_dict["content"] = new_content
        try:
            return ChatMessage.coerce(msg_dict)
        except Exception:
            return msg_dict

    @staticmethod
    def _format_runtime_info(info: dict[str, Any]) -> str:
        import sys
        from datetime import datetime

        from modex_agent.utils.timezone import get_user_timezone

        lines = ["## Runtime"]
        current_time = str(
            info.get("current_time") or datetime.now(get_user_timezone()).strftime("%Y-%m-%d %Hh")
        )
        lines.append(f"Current Time: {current_time} (hour precision, not exact)")

        platform_raw = str(info.get("platform") or sys.platform)
        platform_name = {"win32": "Windows", "darwin": "macOS", "linux": "Linux"}.get(
            platform_raw, platform_raw
        )
        lines.append(f"Platform: {platform_name}")
        return "\n".join(lines)
