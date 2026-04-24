"""AgentPipeline - 端到端流程编排

提供 AgentPipeline 类，统一编排完整的输入→处理→输出流程。
"""

import asyncio
import contextlib
import logging
from typing import Any

from framework.core.skills import SkillManager
from framework.memory.core.message import ChatMessage

from ..core.agent import Agent, AgentContext
from ..core.context import ContextManager
from ..core.emitter import AgentResult, StreamingAwareEmitter
from ..core.runtime_context import RuntimeContextManager
from ..core.tool_manager import ToolManager
from ..core.types import InputMessage, MessageRole
from ..memory import ContextGovernance
from ..memory.consolidation import DreamEngine
from ..memory.history import (
    ListMessageHistory,
    ShortTermMessageHistory,
    history_to_list,
    inject_attachments_to_history,
)
from ..multi_agent import SubagentManager, AgentMessageRouter, MessageDeduplicator, MultiAgentContextBuilder, \
    AgentDescriptor
from ..session.agent_session import _dream_locks
from .adapters import InputAdapter, OutputAdapter, OutputMessage

logger = logging.getLogger(__name__)

_UNSET = object()


class AgentPipeline:
    """Agent 流水线 - 编排完整的端到端流程

    支持多种输入源 → Agent → 多种输出源
    支持流式和非流式两种输出模式

    流程：
    1. InputAdapter 接收输入
    2. ContextManager 加载/构建上下文
    3. Agent 执行推理
    4. Emitter 分发输出事件
    5. OutputAdapter 发送到目标
    6. ContextManager 保存结果
    """

    def __init__(
        self,
        agent: Agent,
        context_manager: ContextManager,
        tool_manager: ToolManager,
        input_adapter: InputAdapter,
        output_adapter: OutputAdapter,
        emitter_factory: Any | None = None,
        dream_engine: DreamEngine | None = None,
        dream_interval: float | None = None,
        dream_threshold: int = 5,
        max_iterations: int = 10,
        incremental_flush: bool = True,
        skill_manager: SkillManager | None = None,
        hooks: list[Any] | None = None,
        subagent_manager: SubagentManager | None = None,
        command_interceptor: Any | None = None,
        router: AgentMessageRouter | None = None,
        deduplicator: MessageDeduplicator | None = None,
        context_builder: MultiAgentContextBuilder | None = None,
        agent_descriptor: AgentDescriptor | None = None,
        sanitizer: Any = _UNSET,
        context_manager_factory: Any | None = None,
        on_session_start: Any | None = None,
        on_session_end: Any | None = None,
        runtime_context_manager: RuntimeContextManager | None = None,
        governance: ContextGovernance | None = None,
    ):
        """
        Args:
            agent: Agent 实例
            context_manager: 上下文管理器
            tool_manager: 工具管理器
            input_adapter: 输入适配器
            output_adapter: 输出适配器
            emitter_factory: 可选的 Emitter 工厂函数，接收 session_id 返回 ContentEmitter
            dream_engine: 可选的 DreamEngine，用于离线长期记忆整理
            dream_interval: 后台扫描周期（秒），None 表示不扫描
            dream_threshold: 触发 DreamEngine 的历史未处理条目阈值
            skill_manager: 可选的 SkillManager，用于构建系统提示词
            hooks: 可选的 AgentRunHook 列表
            subagent_manager: 可选的 SubagentManager，用于 Turn 结束时取消子 Agent
            command_interceptor: 可选的命令拦截器
            router: 可选的 AgentMessageRouter
            deduplicator: 可选的 MessageDeduplicator
            context_builder: 可选的 MultiAgentContextBuilder
            agent_descriptor: 可选的 AgentDescriptor（与 context_builder 配合使用）
            sanitizer: 可选的内容清洗函数，默认使用 ContentSanitizer.sanitize，传 None 表示禁用
            context_manager_factory: 可选的 ContextManager 工厂函数，接收 session_id 返回 ContextManager
            runtime_context_manager: 可选的 RuntimeContextManager，用于按会话隔离运行时状态
            governance: 可选的 ContextGovernance，用于轮内消息治理
        """
        if sanitizer is _UNSET:
            from framework.multi_agent.sanitizer import ContentSanitizer

            sanitizer = ContentSanitizer.sanitize
        self.agent = agent
        self.context_manager = context_manager
        self.tool_manager = tool_manager
        self.input_adapter = input_adapter
        self.output_adapter = output_adapter
        self.emitter_factory = emitter_factory
        self.dream_engine = dream_engine
        self.dream_interval = dream_interval
        self.dream_threshold = dream_threshold
        self.max_iterations = max_iterations
        self.incremental_flush = incremental_flush
        self.skill_manager = skill_manager
        self.hooks = list(hooks) if hooks else []
        if runtime_context_manager is not None:
            from ..core.hooks import RuntimeContextHook
            if not any(isinstance(h, RuntimeContextHook) for h in self.hooks):
                self.hooks.insert(0, RuntimeContextHook())
        self.subagent_manager = subagent_manager
        self.command_interceptor = command_interceptor
        self.router = router
        self.deduplicator = deduplicator
        self.context_builder = context_builder
        self.agent_descriptor = agent_descriptor
        self.sanitizer = sanitizer
        self.context_manager_factory = context_manager_factory
        self.on_session_start = on_session_start
        self.on_session_end = on_session_end
        self.runtime_context_manager = runtime_context_manager
        self.governance = governance
        self._running = False
        self._dream_task: asyncio.Task | None = None
        self._session_locks: dict[str, asyncio.Lock] = {}

    async def run(self) -> None:
        """运行流水线"""
        self._running = True
        await self.input_adapter.start()
        await self.tool_manager.startup()

        if (
            self.dream_engine is not None
            and self.dream_interval is not None
            and self.dream_interval > 0
        ):
            self._dream_task = asyncio.create_task(self._dream_scan_loop())

        try:
            async for input_msg in self.input_adapter.receive():
                if not self._running:
                    break

                try:
                    await self._process_message(input_msg)
                except Exception as e:
                    logger.exception(f"Failed to process message: {e}")
                    # 发送错误响应
                    try:
                        await self.output_adapter.send(
                            OutputMessage(
                                content=f"Error: {str(e)}",
                                message_type="error",
                            ),
                            input_msg.session_id,
                        )
                    except Exception as send_err:
                        logger.error(f"Failed to send error message: {send_err}")
        except asyncio.CancelledError:
            # 正常停止，不记录错误
            logger.info("Pipeline cancelled, shutting down...")
            raise
        except Exception as e:
            logger.exception(f"Pipeline error: {e}")
            raise
        finally:
            if self._dream_task is not None:
                self._dream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._dream_task
                self._dream_task = None
            await self.input_adapter.stop()
            await self.tool_manager.shutdown()

    async def _dream_scan_loop(self) -> None:
        """后台周期性扫描活跃 Context 并触发 DreamEngine。"""
        dream_engine = self.dream_engine
        dream_interval = self.dream_interval
        if dream_engine is None or dream_interval is None:
            return

        while self._running:
            try:
                await asyncio.sleep(dream_interval)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            # Duck typing: MemorySystemContextManager provides get_active_contexts + memory_system
            get_active = getattr(self.context_manager, "get_active_contexts", None)
            memory_system = getattr(self.context_manager, "memory_system", None)
            if get_active is None or memory_system is None:
                continue
            for ctx in self.context_manager.get_active_contexts():
                try:
                    count = await memory_system.get_unprocessed_history_count(ctx)
                except Exception as scan_err:
                    logger.debug("DreamEngine scan error for %s: %s", ctx.session_id, scan_err)
                    continue
                if count >= self.dream_threshold:
                    scope_key = f"{ctx.session_id or ''}:{ctx.user_id or ''}:{ctx.tenant_id or ''}"
                    lock = _dream_locks.setdefault(scope_key, asyncio.Lock())

                    async def _run_dream(
                        c: Any = ctx,
                        engine: Any = dream_engine,
                        lk: Any = lock,
                    ) -> None:
                        async with lk:
                            try:
                                await engine.run(c)
                            except Exception as dream_err:
                                logger.warning("DreamEngine failed: %s", dream_err)

                    asyncio.create_task(_run_dream())

    async def process_message(self, input_msg: InputMessage) -> AgentResult | None:
        """公共入口：处理单个消息"""
        return await self._process_message(input_msg)

    async def _process_message(self, input_msg: InputMessage) -> AgentResult | None:
        """处理单个消息（内部入口）"""
        # 消息路由
        if self.router is not None:
            route_result = self.router.route(input_msg)
            session_id = route_result.agent_session_id
        else:
            route_result = None
            session_id = input_msg.session_id
        logger.info(f"Processing message: session_id={session_id}")

        # 去重检查
        if self.deduplicator is not None:
            message_id = input_msg.metadata.get("message_id") if input_msg.metadata else None
            if not message_id:
                import hashlib

                message_id = hashlib.sha256(
                    f"{session_id}:{input_msg.content}".encode()
                ).hexdigest()[:32]
            if self.deduplicator.is_duplicate(message_id):
                logger.info("Duplicate message skipped: %s", message_id)
                return None

        # 获取或创建 session 级别的锁，防止同一 session 并发处理
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            return await self._process_message_locked(input_msg, session_id, route_result)

    async def _process_message_locked(
        self, input_msg: InputMessage, session_id: str, route_result: Any | None = None
    ) -> AgentResult | None:
        """在 session 锁保护内处理单个消息"""
        if self.on_session_start is not None:
            try:
                await self.on_session_start(session_id)
            except Exception:
                logger.exception("on_session_start failed for %s", session_id)
        ctx_mgr = (
            self.context_manager_factory(session_id)
            if self.context_manager_factory
            else self.context_manager
        )
        input_metadata = getattr(input_msg, "metadata", None) or {}

        # 输入内容清洗
        sanitized_content = input_msg.content
        if self.sanitizer is not None:
            sanitized_content = self.sanitizer(sanitized_content)
            if sanitized_content != input_msg.content:
                logger.info("Input content sanitized for session %s", session_id)

        # 处理附件（通用媒体类型，不限于图片）
        attachments = getattr(input_msg, "attachments", None) or []
        media_blocks: list[Any] = []
        _media_processor = None
        if attachments:
            try:
                from framework.utils.media_utils import MediaProcessor

                _media_processor = MediaProcessor()
                media_result = await _media_processor.process(attachments)
                if media_result.document_text:
                    sanitized_content = (
                        f"{sanitized_content}\n\n{media_result.document_text}".strip()
                        if sanitized_content
                        else media_result.document_text
                    )
                media_blocks = media_result.media_blocks
            except Exception as e:
                logger.warning("Attachment processing failed for session %s: %s", session_id, e)

        # 应用路由的 prompt modifier（agent 消息跳过，前缀由 to_messages() 统一注入）
        source_agent = input_metadata.get("source_agent")
        if not source_agent and route_result and route_result.prompt_modifier:
            sanitized_content = route_result.prompt_modifier + sanitized_content

        # 命令拦截（优先尝试异步版本）
        if self.command_interceptor is not None:
            try:
                handle_async = getattr(self.command_interceptor, "handle_async", None)
                if handle_async is not None:
                    intercept_result = await handle_async(
                        InputMessage(
                            content=sanitized_content,
                            session_id=session_id,
                            metadata=input_metadata,
                        )
                    )
                else:
                    intercept_result = self.command_interceptor.handle(
                        InputMessage(
                            content=sanitized_content,
                            session_id=session_id,
                            metadata=input_metadata,
                        )
                    )
            except Exception:
                logger.exception("CommandInterceptor failed for session %s", session_id)
                intercept_result = None
            if intercept_result is not None:
                await self.output_adapter.send(
                    OutputMessage(content=intercept_result, message_type="command_response"),
                    session_id,
                )
                return

        context_state = await ctx_mgr.load_with_metadata(
            session_id,
            metadata={"input_metadata": input_metadata},
        )

        # 崩溃恢复
        recovered = await ctx_mgr.load_checkpoint(session_id)
        if recovered:
            # ctx_mgr.save(user_message=None) 不会写入 assistant_result.messages，
            # 因此 recovered 消息必须通过 memory_system.add_messages() 直接写入，
            # 否则 clear_checkpoint() 后消息永久丢失。
            memory_system = getattr(ctx_mgr, "memory_system", None)
            if memory_system is not None:
                ctx = getattr(ctx_mgr, "_context_cache", {}).get(session_id)
                if ctx is None:
                    from framework.memory.core.scope import MemoryContext

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

        # 根据 source_agent 区分 agent 间通信消息和用户消息
        # 如果有媒体附件，构建多模态 content（OpenAI 兼容格式）
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

        # 预先保存用户消息
        await ctx_mgr.save(
            session_id=session_id,
            user_message=user_message,
            assistant_result=AgentResult(),
            metadata={"input_metadata": input_metadata},
        )

        # 重新加载以获取最新历史（必须在 build_system_prompt 之前，
        # 否则 load() 会覆盖已注入 tools/skills 的 system_prompt）
        context_state = await ctx_mgr.load(session_id)

        # 恢复当前用户消息的完整多模态内容
        #（memory 中保存的是 sanitize 后的占位符，LLM 需要看到完整媒体）
        if media_blocks and _media_processor is not None:
            from ..memory.history import restore_multimodal_in_history

            pending = await restore_multimodal_in_history(
                context_state.history, multimodal_content, logger
            )
            if pending is not None:
                context_state.history = ListMessageHistory(pending)

        # 构建系统提示词（注入 tools / skills / runtime info）
        agent_name = self.agent_descriptor.address.name if self.agent_descriptor else "main"
        context_state.system_prompt = await ctx_mgr.build_system_prompt(
            tool_manager=self.tool_manager,
            skill_manager=self.skill_manager,
            runtime_info={"caller_context": {"agent_name": agent_name}},
        )

        # 使用 MultiAgentContextBuilder 构建上下文（如果配置）
        if self.context_builder is not None and self.agent_descriptor is not None:
            from ..multi_agent.address import AgentAddress
            from ..multi_agent.envelope import AgentMessageEnvelope

            envelope = AgentMessageEnvelope(
                payload={"content": multimodal_content},
                source=AgentAddress(kind="user", name=input_msg.sender_id or "unknown"),
                target=AgentAddress(
                    kind="agent", name=route_result.agent_name if route_result else "main"
                ),
                message_type=route_result.envelope_metadata.get("message_type", "agent_message")
                if route_result
                else "agent_message",
                conversation_id=route_result.conversation_id if route_result else session_id,
                agent_session_id=session_id,
                metadata=input_metadata,
            )
            base_history = await history_to_list(context_state.history)
            if base_history and base_history[-1].get("role") == MessageRole.USER:
                base_history = base_history[:-1]
            built_messages = self.context_builder.build_messages(
                history=base_history,
                current_envelope=envelope,
                agent_descriptor=self.agent_descriptor,
            )
            system_msgs = [m for m in built_messages if m.get("role") == "system"]
            if system_msgs:
                context_state.system_prompt = "\n\n".join(m.get("content", "") for m in system_msgs)
            non_system = [m for m in built_messages if m.get("role") != "system"]
            # Defensive: ensure the current user message is preserved in history.
            # If context_builder.build_messages() omits the user message,
            # replace_all() would permanently lose it.
            if user_message.get("role") == MessageRole.USER and not any(
                m.get("role") == MessageRole.USER for m in non_system
            ):
                non_system = list(non_system) + [user_message]
            # Write built non-system messages back into the underlying MessageHistory
            # ShortTermMessageHistory 支持 replace_all() 原子写入存储，
            # 避免替换为 ListMessageHistory 导致实时写入中断。
            if isinstance(context_state.history, ShortTermMessageHistory):
                await context_state.history.replace_all(non_system)
            else:
                context_state.history = ListMessageHistory(non_system)

        async def on_checkpoint(messages: list[ChatMessage | dict[str, Any]]) -> None:
            await ctx_mgr.save_checkpoint(session_id, messages)

        agent_context = AgentContext(
            system_prompt=context_state.system_prompt,
            history=context_state.history,
            tool_manager=self.tool_manager,
            session_id=session_id,
            max_iterations=self.max_iterations,
            metadata={"session_id": session_id},
            on_checkpoint=on_checkpoint,
            hooks=self.hooks,
            runtime_context_manager=self.runtime_context_manager,
            governance=self.governance,
        )

        # 选择 emitter：
        # - main agent（有 emitter_factory）→ 工厂 emitter（QQBotEmitter 等）
        # - peer agent（无 emitter_factory）→ StreamingAwareEmitter
        if self.emitter_factory:
            emitter = self.emitter_factory(session_id)
        else:
            emitter = StreamingAwareEmitter(
                output_adapter=self.output_adapter,
                session_id=session_id,
            )

        # 设置当前 conversation_id 上下文变量（供 peer 通信工具使用）
        from ..multi_agent.subagent_manager import current_conversation_id

        agent_name = self.agent_descriptor.address.name if self.agent_descriptor else "main"
        conversation_id = input_metadata.get("conversation_id", session_id) or session_id
        conv_token = current_conversation_id.set(conversation_id)
        result: AgentResult | None = None

        try:
            result = await self.agent.run(agent_context, emitter)

            # 为最后一条 assistant 消息注入 attachments metadata
            if result and result.attachments:
                await inject_attachments_to_history(
                    context_state.history, result.attachments
                )

            await ctx_mgr.save(
                session_id=session_id,
                user_message=None,
                assistant_result=result,
                metadata={"input_metadata": input_metadata},
            )
        finally:
            current_conversation_id.reset(conv_token)
            await ctx_mgr.flush(session_id)
            await ctx_mgr.clear_checkpoint(session_id)
            # Turn 结束时的清理工作（同步 subagent 无需 cancel_by_session）
            if self.on_session_end is not None:
                try:
                    await self.on_session_end(session_id)
                except Exception:
                    logger.exception("on_session_end failed for %s", session_id)

        return result

    async def stop(self) -> None:
        """停止流水线"""
        self._running = False
        logger.info("Pipeline stop requested, waiting for current message to complete...")


