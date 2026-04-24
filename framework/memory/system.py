"""MemorySystem and adapter for ContextManager integration."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from framework.core.context import ContextManager, ContextState
from framework.core.emitter import AgentResult
from framework.core.skills import ResolutionContext, SkillManager
from framework.memory.archive import SemanticArchiveStrategy
from framework.memory.core.base_managers import (
    BaseHistoryArchiveManager,
    BaseLongTermMemoryManager,
    BaseShortTermManager,
)
from framework.memory.core.message import ChatMessage
from framework.memory.core.scope import (
    CompositeScope,
    MemoryContext,
    MemoryScope,
    SessionScope,
    TenantScope,
    UserScope,
)
from framework.memory.core.storage import MemoryStorage
from framework.memory.managers.history import HistoryArchiveManager
from framework.memory.managers.long_term import LongTermMemory, LongTermMemoryManager
from typing import cast

from framework.memory.history import ShortTermMessageHistory
from framework.memory.managers.short_term import ShortTermConfig, ShortTermMemoryManager
from framework.memory.content_transform import ContentTransformer
from framework.memory.recorder import MemoryAppendRecorder
from framework.memory.stores.file import FileStorage
from framework.memory.stores.in_memory import InMemoryStorage

logger = logging.getLogger(__name__)


@dataclass
class MemorySystemManagers:
    """MemorySystem 内四级记忆管理器的结构化容器。

    替代 dict[str, Any]，提供类型安全和 IDE 自动补全。
    插件可通过替换 short_term 字段注入自定义实现。
    """

    short_term: BaseShortTermManager
    history: BaseHistoryArchiveManager | None = None
    long_term: BaseLongTermMemoryManager | None = None


def _derive_memory_budget(llm_max_tokens: int | None, budget_ratio: float = 0.5) -> tuple[int, int]:
    """根据 LLM 的 max_tokens 推导短期记忆的预算上限。

    Returns:
        (max_messages, max_tokens) 元组。max_messages 固定为 100，
        max_tokens = int(llm_max_tokens * budget_ratio)，最高不超过 128000。
        当 llm_max_tokens 为 None 时，回退到 legacy 默认值 (100, 8000)。
    """
    if llm_max_tokens is None:
        return 100, 8000
    max_tokens = int(llm_max_tokens * budget_ratio)
    cap = 128000
    if max_tokens > cap:
        max_tokens = cap
    return 100, max_tokens


@dataclass
class LayerConfig:
    """记忆层配置。"""

    scope: MemoryScope
    storage: MemoryStorage
    archive_strategy: Any | None = None
    max_messages: int | None = 100
    max_tokens: int | None = 8000
    max_entries: int | None = None
    content_transformer: ContentTransformer | None = None
    pipeline: Any | None = None  # MemoryCompactionPipeline for short_term layer


class MemorySystem:
    """统一的多层记忆系统入口。

    协调 Short-term、History、Long-term 三层记忆管理器，
    支持每层独立配置分组维度和存储后端。
    """

    def __init__(
        self,
        workspace: Path | None = None,
        layers: dict[str, LayerConfig] | None = None,
        llm_provider: Any | None = None,
        auto_llm_compression: bool = True,
    ):
        self.workspace = Path(workspace) if workspace else Path("./memory")
        self.layers = layers or self.default_single_user_layers(
            self.workspace, llm_provider=llm_provider, auto_llm_compression=auto_llm_compression
        )
        self._managers: MemorySystemManagers = self._build_managers()
        self._recorder = MemoryAppendRecorder()

    def get_providers(self) -> list[Any]:
        """Return a snapshot of registered memory providers."""
        return list(self._recorder.providers)

    @property
    def _providers(self) -> list[Any]:
        """Internal accessor for tests that inspect registered providers."""
        return self._recorder.providers

    def _build_managers(self) -> MemorySystemManagers:
        # Short-term layer (required)
        history: HistoryArchiveManager | None = None
        if "history" in self.layers:
            history = HistoryArchiveManager(
                self.layers["history"].storage,
                self.layers["history"].scope,
                max_entries=self.layers["history"].max_entries,
            )

        # Wire history_manager into pipeline if not already set
        pipeline = self.layers["short_term"].pipeline
        if pipeline is not None and history is not None:
            if getattr(pipeline, "_history_manager", None) is None:
                pipeline._history_manager = history
            if getattr(pipeline, "_archive_strategy", None) is None:
                pipeline._archive_strategy = self.layers["short_term"].archive_strategy

        short_term = ShortTermMemoryManager(
            self.layers["short_term"].storage,
            self.layers["short_term"].scope,
            config=ShortTermConfig(
                max_messages=self.layers["short_term"].max_messages,
                max_tokens=self.layers["short_term"].max_tokens,
                archive_strategy=self.layers["short_term"].archive_strategy,
                content_transformer=self.layers["short_term"].content_transformer,
                pipeline=pipeline,
            ),
            history_manager=history,
        )

        # Long-term layer (optional)
        long_term: LongTermMemoryManager | None = None
        if "long_term" in self.layers:
            long_term = LongTermMemoryManager(
                self.layers["long_term"].storage,
                self.layers["long_term"].scope,
            )

        return MemorySystemManagers(
            short_term=short_term,
            history=history,
            long_term=long_term,
        )

    @classmethod
    def default_single_user_layers(
        cls,
        workspace: Path | None = None,
        llm_provider: Any | None = None,
        auto_llm_compression: bool = True,
        short_term_max_messages: int | None = None,
        short_term_max_tokens: int | None = None,
        llm_max_tokens: int | None = None,
        budget_ratio: float = 0.5,
    ) -> dict[str, LayerConfig]:
        """单用户桌面场景默认配置。"""
        ws = Path(workspace) if workspace else Path("./memory")
        file_store = FileStorage(ws)

        pipeline = None
        archive_strategy = None
        if auto_llm_compression and llm_provider is not None:
            from framework.memory.compaction.pipeline import (
                ConsolidatorSummaryStrategy,
                MemoryCompactionPipeline,
            )
            from framework.memory.consolidation.consolidator import Consolidator

            consolidator = Consolidator(llm_provider=llm_provider)
            archive_strategy = SemanticArchiveStrategy()
            pipeline = MemoryCompactionPipeline(
                summary_strategy=ConsolidatorSummaryStrategy(consolidator),
                archive_strategy=archive_strategy,
            )

        max_messages = short_term_max_messages if short_term_max_messages is not None else 100
        if short_term_max_tokens is not None:
            max_tokens = short_term_max_tokens
        else:
            _, max_tokens = _derive_memory_budget(llm_max_tokens, budget_ratio=budget_ratio)

        return {
            "short_term": LayerConfig(
                scope=SessionScope(),
                storage=file_store,
                archive_strategy=archive_strategy,
                max_messages=max_messages,
                max_tokens=max_tokens,
                pipeline=pipeline,
            ),
            "history": LayerConfig(
                scope=UserScope(), storage=file_store, max_entries=1000
            ),
            "long_term": LayerConfig(scope=UserScope(), storage=file_store),
        }

    @classmethod
    def default_multi_tenant_layers(
        cls,
        workspace: Path | None = None,
        llm_provider: Any | None = None,
        auto_llm_compression: bool = True,
    ) -> dict[str, LayerConfig]:
        """多租户 SaaS 场景默认配置。"""
        ws = Path(workspace) if workspace else Path("./memory")
        file_store = FileStorage(ws)

        pipeline = None
        archive_strategy = None
        if auto_llm_compression and llm_provider is not None:
            from framework.memory.compaction.pipeline import (
                ConsolidatorSummaryStrategy,
                MemoryCompactionPipeline,
            )
            from framework.memory.consolidation.consolidator import Consolidator

            consolidator = Consolidator(llm_provider=llm_provider)
            archive_strategy = SemanticArchiveStrategy()
            pipeline = MemoryCompactionPipeline(
                summary_strategy=ConsolidatorSummaryStrategy(consolidator),
                archive_strategy=archive_strategy,
            )

        return {
            "short_term": LayerConfig(
                scope=CompositeScope(TenantScope(), UserScope(), SessionScope()),
                storage=file_store,
                archive_strategy=archive_strategy,
                pipeline=pipeline,
            ),
            "history": LayerConfig(
                scope=CompositeScope(TenantScope(), UserScope()),
                storage=file_store,
                max_entries=1000,
            ),
            "long_term": LayerConfig(
                scope=CompositeScope(TenantScope(), UserScope()),
                storage=file_store,
            ),
        }

    async def initialize(self) -> None:
        """初始化所有存储后端。"""
        for config in self.layers.values():
            await config.storage.initialize()

    async def close(self) -> None:
        """关闭所有存储后端和注册的 providers。"""
        for config in self.layers.values():
            await config.storage.close()
        await self._recorder.flush()
        for provider in self._recorder.providers:
            try:
                await provider.shutdown()
            except Exception as e:
                logger.warning("Provider '%s' shutdown error: %s", provider.name, e)

    def create_message_history(
        self,
        context: MemoryContext,
        initial_messages: Sequence[ChatMessage | dict[str, Any]] | None = None,
    ) -> ShortTermMessageHistory:
        """创建与短期记忆关联的 MessageHistory 实例。

        通过工厂方法解耦 InjectionPolicy 对具体 manager 类型的依赖。
        """
        stm = self._managers.short_term
        if stm is None:
            raise RuntimeError("short_term layer is not configured")
        return ShortTermMessageHistory(
            manager=cast(ShortTermMemoryManager, stm),
            context=context,
            initial_messages=initial_messages,
            recorder=self._recorder,
        )

    async def add_message(
        self,
        context: MemoryContext,
        message: ChatMessage | dict[str, Any],
    ) -> None:
        """添加消息到短期记忆。"""
        await self.add_messages(context, [message])

    def add_provider(self, provider: Any) -> None:
        """Add a MemoryProvider (called by PluginLoader)."""
        self._recorder.add_provider(provider)
        # Wire on_pre_compress callback into short-term manager
        stm = self._managers.short_term
        if stm is not None:
            stm.add_pre_compress_callback(provider.on_pre_compress)

    async def search_memories(
        self,
        query: str,
        context: MemoryContext,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Aggregate search results from all registered providers.

        Fan-out to all providers, per-provider error isolation.
        Results are sorted by score descending and truncated to limit.
        Per-provider min-max normalization is applied so scores from different
        providers (with potentially different scales) can be compared fairly.
        """
        provider_results: list[tuple[str, list[dict[str, Any]]]] = []
        for provider in self._recorder.providers:
            try:
                results = await provider.search(query, context, limit)
                if results:
                    provider_results.append((provider.name, results))
            except Exception as e:
                logger.warning("Provider '%s' search failed: %s", provider.name, e)

        # Normalize scores per-provider, then merge and sort
        normalized: list[tuple[float, dict[str, Any]]] = []
        for _provider_name, results in provider_results:
            scores = [r.get("score", 0) for r in results]
            min_score = min(scores)
            max_score = max(scores)
            score_range = max_score - min_score
            for i, result in enumerate(results):
                if score_range > 0:
                    norm = (scores[i] - min_score) / score_range
                else:
                    norm = 0.5  # all equal within provider
                normalized.append((norm, result))

        normalized.sort(key=lambda x: x[0], reverse=True)
        return [r for _score, r in normalized[:limit]]

    async def add_messages(
        self,
        context: MemoryContext,
        messages: Sequence[ChatMessage | dict[str, Any]],
    ) -> None:
        """批量添加消息到短期记忆，并 fan-out 到所有注册的 MemoryProvider。"""
        if not messages:
            return
        # Debug: validate source_agent consistency with scope key sender
        if context.session_id and ":" in context.session_id:
            parts = context.session_id.split(":")
            if len(parts) == 3:
                scope_sender = parts[1]
                for msg in messages:
                    msg_sender = msg.get("source_agent")
                    if msg_sender is not None and msg_sender != scope_sender:
                        logger.debug(
                            "Source agent mismatch: scope says %s, msg says %s for session %s",
                            scope_sender,
                            msg_sender,
                            context.session_id,
                        )
        await self._managers.short_term.add_messages(context, messages)

        # Fan-out to registered providers via recorder (fire-and-forget)
        await self._recorder.record(list(messages), context)

    async def prefetch_memories(
        self,
        query: str,
        context: MemoryContext,
    ) -> str | None:
        """Aggregate prefetch results from all registered providers.

        Called by DefaultMemoryInjectionPolicy to inject relevant memories
        into the LLM context before each turn.
        """
        blocks: list[str] = []
        for provider in self._recorder.providers:
            try:
                block = await provider.prefetch(query, context)
                if block:
                    blocks.append(block)
            except Exception as e:
                logger.warning("Provider '%s' prefetch failed: %s", provider.name, e)
        return "\n\n".join(blocks) if blocks else None

    async def get_history(
        self, context: MemoryContext, max_messages: int | None = None
    ) -> list[ChatMessage]:
        """获取短期记忆历史（供 adapter load 使用）。"""
        msgs = await self._managers.short_term.get_messages(context)
        if max_messages and len(msgs) > max_messages:
            return msgs[-max_messages:]
        return msgs

    async def get_compression_summary(self, context: MemoryContext) -> str | None:
        """获取短期记忆的压缩摘要（若存在）。"""
        return await self._managers.short_term.get_compression_summary(context)

    async def get_auto_compact_summary(self, context: MemoryContext) -> str | None:
        """获取 AutoCompact 生成的空闲压缩摘要（若存在）。"""
        scope_key = self.layers["short_term"].scope.get_scope_key(context)
        result = await self.layers["short_term"].storage.get(scope_key, ".auto_compact_summary")
        return result if isinstance(result, str) else None

    async def get_history_entries(
        self, context: MemoryContext, limit: int = 5
    ) -> list[dict[str, Any]]:
        """获取历史摘要条目（HistoryArchiveManager）。"""
        history_mgr = self._managers.history
        if history_mgr is None:
            return []
        return await history_mgr.get_recent(context, limit=limit)

    async def get_long_term(self, context: MemoryContext) -> LongTermMemory:
        """获取长期记忆内容。"""
        long_term_mgr = self._managers.long_term
        if long_term_mgr is None:
            return LongTermMemory()
        return await long_term_mgr.get_all(context)

    async def build_system_prompt(
        self,
        context: MemoryContext,
        max_history_entries: int = 5,
        query: str = "",
    ) -> str:
        """构建包含长期记忆和近期历史摘要的系统提示词。

        Args:
            context: 记忆上下文
            max_history_entries: 最多包含的历史摘要条目数
            query: 用户查询字符串。非空时通过 search() 检索相关历史，
                   否则回退到 get_recent() 盲取最近条目。
        """
        sections: list[str] = []

        # 长期记忆（可选）
        long_term_mgr = self._managers.long_term
        if long_term_mgr is not None:
            long_term = await long_term_mgr.get_all(context)
            if long_term.soul:
                sections.append(f"## 你的沟通风格\n{long_term.soul}")
            if long_term.user:
                sections.append(f"## 用户画像\n{long_term.user}")
            if long_term.memory:
                sections.append(f"## 相关知识\n{long_term.memory}")
            for key, value in long_term.custom.items():
                sections.append(f"## {key}\n{value}")

        # 注入中期记忆（历史摘要，可选）
        if max_history_entries > 0:
            history_mgr = self._managers.history
            if history_mgr is not None:
                if query:
                    history_entries = await history_mgr.search(
                        context, query=query, limit=max_history_entries
                    )
                    # 检索无结果时回退到最近条目，避免丢失历史上下文
                    if not history_entries:
                        history_entries = await history_mgr.get_recent(
                            context, limit=max_history_entries
                        )
                else:
                    history_entries = await history_mgr.get_recent(
                        context, limit=max_history_entries
                    )
                if history_entries:
                    history_lines: list[str] = []
                    for entry in history_entries:
                        summary = entry.get("summary", "")
                        if summary:
                            history_lines.append(f"- {summary}")
                    if history_lines:
                        sections.append("## 历史对话摘要\n" + "\n".join(history_lines))

        return "\n\n---\n\n".join(sections) if sections else ""

    async def get_unprocessed_history_count(
        self, context: MemoryContext, cursor_name: str = "dream"
    ) -> int:
        """获取未处理的历史摘要条目数量。"""
        history_mgr = self._managers.history
        if history_mgr is None:
            return 0
        _, entries = await history_mgr.get_unprocessed(context, cursor_name)
        return len(entries)

    @property
    def history_manager(self) -> BaseHistoryArchiveManager | None:
        """暴露 HistoryArchiveManager，供 DreamEngine 等外部组件使用。"""
        return self._managers.history

    @property
    def long_term_manager(self) -> BaseLongTermMemoryManager | None:
        """暴露 LongTermMemoryManager，供 DreamEngine 等外部组件使用。"""
        return self._managers.long_term

    async def save_checkpoint(self, context: MemoryContext, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        """保存崩溃恢复检查点（收口到 ShortTermMemoryManager）。"""
        await self._managers.short_term.save_checkpoint(context, messages)

    async def load_checkpoint(self, context: MemoryContext) -> list[ChatMessage] | None:
        """加载崩溃恢复检查点。"""
        return await self._managers.short_term.load_checkpoint(context)

    async def clear_checkpoint(self, context: MemoryContext) -> None:
        """清除崩溃恢复检查点。"""
        await self._managers.short_term.clear_checkpoint(context)

    async def clear(self, context: MemoryContext) -> None:
        """清空指定 scope 的所有记忆层级。"""
        # 短期记忆
        await self._managers.short_term.clear_messages(context)
        # 历史摘要（可选）
        if "history" in self.layers:
            history_scope_key = self.layers["history"].scope.get_scope_key(context)
            await self.layers["history"].storage.save_logs(history_scope_key, [])
        # 长期记忆（可选）
        long_term_mgr = self._managers.long_term
        if long_term_mgr is not None:
            await long_term_mgr.clear(context)
        # 检查点
        await self.clear_checkpoint(context)


class MemorySystemContextManager(ContextManager):
    """适配器：将 MemorySystem 包装为现有的 ContextManager 接口。

    这是一个**过渡性**适配器。未来新代码应直接使用 MemorySystem，
    _legacy ContextManager 接口将被逐步废弃。
    """

    def __init__(
        self,
        memory_system: MemorySystem,
        default_user_id: str = "default",
        base_system_prompt: str = "",
        injection_policy: Any | None = None,
    ):
        from framework.memory.injection import DefaultMemoryInjectionPolicy

        self.memory_system = memory_system
        self.default_user_id = default_user_id
        self.base_system_prompt = base_system_prompt
        self.injection_policy = injection_policy or DefaultMemoryInjectionPolicy()
        self._last_session_id: str | None = None
        self._context_cache: dict[str, MemoryContext] = {}
        self._max_context_cache_size = 1000

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
        # 只有在提供了新的运行时信息时才重新构建上下文；否则复用缓存
        if runtime_info or metadata:
            ctx = self._build_context(session_id, runtime_info=runtime_info, metadata=metadata)
        else:
            ctx = self._context_cache.get(session_id)
            if ctx is None:
                ctx = MemoryContext(session_id=session_id, user_id=self.default_user_id)
        # Ensure context is within budget before LLM request, even if no new
        # messages were added since the last add_messages() call.
        stm = self.memory_system._managers.short_term
        if hasattr(stm, "_maybe_compress"):
            try:
                await stm._maybe_compress(ctx)
            except Exception:
                logger.warning("Pre-load compression check failed", exc_info=True)
        return await self.injection_policy.assemble(
            self.memory_system, ctx, self.base_system_prompt
        )

    def _has_channel_scope(self) -> bool:
        """检查 short_term 层是否配置了 ChannelScope 或包含它的 CompositeScope。"""
        from framework.memory.core.scope import ChannelScope, CompositeScope

        short_term_layer = self.memory_system.layers.get("short_term")
        if short_term_layer is None:
            return False
        scope = short_term_layer.scope
        if isinstance(scope, ChannelScope):
            return True
        if isinstance(scope, CompositeScope):
            return any(isinstance(s, ChannelScope) for s in scope.scopes)
        return False

    def _has_chat_scope(self) -> bool:
        """检查 short_term 层是否配置了 ChatScope 或包含它的 CompositeScope。"""
        from framework.memory.core.scope import ChatScope, CompositeScope

        short_term_layer = self.memory_system.layers.get("short_term")
        if short_term_layer is None:
            return False
        scope = short_term_layer.scope
        if isinstance(scope, ChatScope):
            return True
        if isinstance(scope, CompositeScope):
            return any(isinstance(s, ChatScope) for s in scope.scopes)
        return False

    def _build_context(
        self,
        session_id: str,
        runtime_info: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryContext:
        """从 runtime_info / metadata 中提取真实维度构建 MemoryContext。"""
        input_metadata = metadata.get("input_metadata") if metadata else None

        def _extract(key: str) -> str | None:
            if runtime_info and key in runtime_info:
                return runtime_info.get(key)
            if input_metadata and key in input_metadata:
                return input_metadata.get(key)
            return None

        user_id = _extract("user_id") or self.default_user_id
        tenant_id = _extract("tenant_id")
        channel = _extract("channel")
        chat_id = _extract("chat_id")

        if channel is None and self._has_channel_scope():
            logger.warning("ChannelScope configured but channel is None for session %s", session_id)
        if chat_id is None and self._has_chat_scope():
            logger.warning("ChatScope configured but chat_id is None for session %s", session_id)

        sender_agent = _extract("sender_agent") or _extract("source_agent")
        receiver_agent = _extract("receiver_agent")

        # 如果 session_id 是三段格式，自动解析 sender/receiver
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
            channel=channel,
            chat_id=chat_id,
            sender_agent=sender_agent,
            receiver_agent=receiver_agent,
        )
        if len(self._context_cache) >= self._max_context_cache_size:
            self._context_cache.clear()
        self._context_cache[session_id] = ctx
        return ctx

    async def save(
        self,
        session_id: str,
        user_message: ChatMessage | dict[str, Any] | None,
        assistant_result: AgentResult,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """保存对话回合。

        与 InMemoryContextManager / FileContextManager 保持一致：
        - 只保存 user_message（用户输入）到短期记忆
        - 不保存 assistant_result.messages —— ReActAgent 已通过
          context.history.append() 实时写入 ShortTermMessageHistory
          （即 messages.jsonl），无需 save() 再次追加
        """
        if user_message:
            ctx = self._build_context(session_id, metadata=metadata)
            # IM Bot 运行时上下文前缀注入
            prefixed_message = self._apply_runtime_context_prefix(
                user_message, metadata.get("input_metadata") if metadata else None
            )
            await self.memory_system.add_messages(ctx, [prefixed_message])

    async def flush(self, session_id: str) -> None:
        """Turn-boundary 持久化钩子。

        当前为 no-op：所有消息（包括 ReAct 轮内消息）已通过
        ShortTermMessageHistory 实时写入存储，无需额外 flush。
        保留此接口以兼容现有 pipeline 调用方。
        """

    async def save_checkpoint(self, session_id: str, messages: Sequence[ChatMessage | dict[str, Any]]) -> None:
        """保存崩溃恢复检查点。"""
        ctx = self._context_cache.get(session_id)
        if ctx is None:
            ctx = MemoryContext(session_id=session_id, user_id=self.default_user_id)
        await self.memory_system.save_checkpoint(ctx, messages)

    async def load_checkpoint(self, session_id: str) -> list[ChatMessage] | None:
        """加载崩溃恢复检查点。"""
        ctx = self._context_cache.get(session_id)
        if ctx is None:
            ctx = MemoryContext(session_id=session_id, user_id=self.default_user_id)
        return await self.memory_system.load_checkpoint(ctx)

    async def clear_checkpoint(self, session_id: str) -> None:
        """清除崩溃恢复检查点。"""
        ctx = self._context_cache.get(session_id)
        if ctx is None:
            ctx = MemoryContext(session_id=session_id, user_id=self.default_user_id)
        await self.memory_system.clear_checkpoint(ctx)

    def _apply_runtime_context_prefix(
        self,
        user_message: ChatMessage | dict[str, Any],
        input_metadata: dict[str, Any] | None = None,
    ) -> ChatMessage | dict[str, Any]:
        """将 IM Bot 的运行时上下文（channel/chat_id）注入到用户消息内容前。

        支持 str 和 list[dict]（多模态）两种 content 格式。
        不需要修改时返回原始对象以保持向后兼容。
        """
        if not input_metadata:
            return user_message

        runtime_lines: list[str] = []
        if "channel" in input_metadata:
            runtime_lines.append(f"channel={input_metadata['channel']}")
        if "chat_id" in input_metadata:
            runtime_lines.append(f"chat_id={input_metadata['chat_id']}")

        if not runtime_lines:
            return user_message

        # Work with dict for mutation
        msg_dict = user_message.to_dict() if isinstance(user_message, ChatMessage) else dict(user_message)
        original_content = msg_dict.get("content", "")
        prefix = "[Runtime Context]\n" + "\n".join(runtime_lines) + "\n\n"

        if isinstance(original_content, list):
            if not original_content:
                # 空列表不注入 prefix，返回原始对象以保持 identity
                return user_message
            new_content = [{"type": "text", "text": prefix}] + list(original_content)
        else:
            new_content = prefix + str(original_content)

        msg_dict["content"] = new_content
        # 如果输入是 dict，返回 dict；如果输入是 ChatMessage，返回 ChatMessage
        if isinstance(user_message, ChatMessage):
            return ChatMessage.coerce(msg_dict)
        return msg_dict

    async def build_system_prompt(
        self,
        tool_manager: Any,
        skill_manager: SkillManager | None = None,
        runtime_info: dict[str, Any] | None = None,
    ) -> str:
        # 从 runtime_info 或最近 load 的 session 中获取 session_id
        session_id = (
            runtime_info.get("session_id")
            if runtime_info and "session_id" in runtime_info
            else self._last_session_id
        )
        if not session_id:
            session_id = "default"

        ctx = self._build_context(session_id, runtime_info=runtime_info)

        # 统一注入入口：委托给 injection_policy，避免与 assemble() 重复逻辑
        context_state = await self.injection_policy.assemble(
            self.memory_system, ctx, self.base_system_prompt
        )
        # policy 返回的 system_prompt 已包含 base_system_prompt，不再重复追加
        memory_prompt = context_state.system_prompt

        parts: list[str] = []
        if memory_prompt:
            parts.append(memory_prompt)

        # Skills（tool 描述由 Agent 通过 API tools 参数传递，不注入 system prompt）
        if skill_manager is not None:
            skill_prompt = await skill_manager.build_prompt(
                ResolutionContext.from_runtime(tool_manager=tool_manager)
            )
            if skill_prompt:
                parts.append(skill_prompt)

        # 运行时信息
        if runtime_info:
            runtime_text = self._format_runtime_info(runtime_info)
            if runtime_text:
                parts.append(runtime_text)

        return "\n\n---\n\n".join(parts)

    def get_active_contexts(self) -> list[MemoryContext]:
        """返回当前缓存中的所有活跃 MemoryContext（副本）。"""
        return list(self._context_cache.values())

    async def clear(self, session_id: str) -> None:
        ctx = self._context_cache.get(session_id)
        if ctx is None:
            ctx = MemoryContext(session_id=session_id, user_id=self.default_user_id)
        await self.memory_system.clear(ctx)

    def _format_runtime_info(self, info: dict[str, Any]) -> str:
        lines = ["## Runtime"]
        if "current_time" in info:
            lines.append(f"Current Time: {info['current_time']}")
        if "platform" in info:
            lines.append(f"Platform: {info['platform']}")
        return "\n".join(lines)
