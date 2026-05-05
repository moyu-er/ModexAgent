"""AgentSession 协调层

提供单次请求处理的会话管理，适合 HTTP API 场景。
与 AgentPipeline 相比，AgentSession 更轻量，专注于单次消息处理。
"""

import asyncio
import logging
from typing import Any, Generic, TypeVar

from framework.core.skills import SkillManager

from ..core.agent import Agent, AgentContext
from ..core.context import ContextManager
from ..core.context_extensions import ExtensionKey
from ..core.emitter import AgentResult, ContentEmitter
from ..core.events import AgentEvent
from ..core.graph.interrupt import GraphInterrupt
from ..core.tool_manager import ToolManager
from ..core.types import InputMessage
from ..memory.core.scope import MemoryContext
from ..memory.history import (
    ListMessageHistory,
    MessageHistory,
    history_to_list,
    inject_attachments_to_history,
    restore_multimodal_in_history,
)

E = TypeVar("E", bound=AgentEvent)

# 全局 DreamEngine 执行锁，按 scope 键隔离并发运行
_dream_locks: dict[str, asyncio.Lock] = {}


class AgentSession(Generic[E]):
    """Agent 会话协调器

    职责：
    1. 协调 Agent、ContextManager、ToolManager 的生命周期
    2. 处理单次消息（load → build → run → save）
    3. 提供统一的错误处理

    与 AgentPipeline 的区别：
    - AgentSession: 单次请求处理，适合 HTTP API
    - AgentPipeline: 端到端编排，适合长期运行的服务

    泛型参数 E 是 Agent 特定的事件枚举类型。

    Example:
        session = AgentSession(
            agent=ReActAgent(provider=llm),
            context_manager=InMemoryContextManager(),
            tool_manager=InMemoryToolManager(),
        )

        # 处理单条消息
        result = await session.process_message(
            message=InboundMessage(content="Hello"),
            emitter=BufferingEmitter(),
            session_id="user_123",
        )
    """

    def __init__(
        self,
        agent: Agent[E],
        context_manager: ContextManager | None = None,
        tool_manager: ToolManager | None = None,
        skill_manager: SkillManager | None = None,
        memory_system: Any | None = None,
        dream_engine: Any | None = None,
        dream_threshold: int = 5,
        hooks: list[Any] | None = None,
        router: Any | None = None,
        deduplicator: Any | None = None,
        context_builder: Any | None = None,
        agent_descriptor: Any | None = None,
        sanitizer: Any | None = None,
        command_interceptor: Any | None = None,
        subagent_manager: Any | None = None,
        runtime_context_manager: Any | None = None,
        hook_runner: Any | None = None,
        interceptor_chain: Any | None = None,
        checkpoint_store: Any | None = None,
    ):
        """初始化 AgentSession

        Args:
            agent: Agent 实例（如 ReActAgent）
            context_manager: 上下文管理器（与 memory_system 二选一）
            tool_manager: 工具管理器
            skill_manager: 可选的 SkillManager，用于构建系统提示词
            memory_system: 可选的 MemorySystem，若提供则内部包装为 ContextManager 适配器
            dream_engine: 可选的 DreamEngine，用于离线长期记忆整理
            dream_threshold: 触发 DreamEngine 的历史未处理条目阈值
            hooks: 可选的 AgentRunHook 列表，传递给 AgentContext
            router: 可选的 AgentMessageRouter
            deduplicator: 可选的 MessageDeduplicator
            context_builder: 可选的 MultiAgentContextBuilder
            agent_descriptor: 可选的 AgentDescriptor（与 context_builder 配合使用）
            sanitizer: 可选的内容清洗器（Callable[[str], str]）
            command_interceptor: 可选的命令拦截器
            subagent_manager: 可选的 SubagentManager，用于 turn 结束时取消子 Agent
        """
        if tool_manager is None:
            raise ValueError("tool_manager is required")
        if memory_system is not None and context_manager is not None:
            raise ValueError("Cannot provide both context_manager and memory_system")
        if memory_system is not None:
            from ..memory.system import MemorySystemContextManager

            context_manager = MemorySystemContextManager(memory_system)
        if context_manager is None:
            raise ValueError("Must provide either context_manager or memory_system")
        self._agent = agent
        self._context_manager = context_manager
        self._tool_manager = tool_manager
        self._skill_manager = skill_manager
        self._dream_engine = dream_engine
        self._dream_threshold = dream_threshold
        self._hooks = list(hooks) if hooks else []
        self._router = router
        self._deduplicator = deduplicator
        self._context_builder = context_builder
        self._agent_descriptor = agent_descriptor
        self._sanitizer = sanitizer
        self._command_interceptor = command_interceptor
        self._subagent_manager = subagent_manager
        self._runtime_context_manager = runtime_context_manager
        self._hook_runner = hook_runner
        self._interceptor_chain = interceptor_chain
        self._checkpoint_store = checkpoint_store

    async def process_message(
        self,
        message: InputMessage,
        emitter: ContentEmitter[E],
        session_id: str,
        runtime_info: dict[str, Any] | None = None,
    ) -> AgentResult:
        """处理单条消息

        完整的处理流程：
        1. 加载会话上下文
        2. 构建系统提示词
        3. 构建 AgentContext
        4. 执行 Agent
        5. 保存对话结果

        Args:
            message: 输入消息
            emitter: 内容发送器
            session_id: 会话 ID
            runtime_info: 可选的运行时信息

        Returns:
            AgentResult: 执行结果
        """
        # 消息路由
        route_result = None
        if self._router is not None:
            route_result = self._router.route(message)
            session_id = route_result.agent_session_id

        # 去重检查
        if self._deduplicator is not None:
            message_id = message.metadata.get("message_id") if message.metadata else None
            if not message_id:
                import hashlib
                message_id = hashlib.sha256(f"{session_id}:{message.content}".encode()).hexdigest()[:32]
            if self._deduplicator.is_duplicate(message_id):
                logger = logging.getLogger(__name__)
                logger.info("Duplicate message skipped: %s", message_id)
                return AgentResult(content="", stop_reason="duplicate")

        # 输入内容清洗
        sanitized_content = message.content
        if self._sanitizer is not None:
            sanitized_content = self._sanitizer(sanitized_content)
            if sanitized_content != message.content:
                logger = logging.getLogger(__name__)
                logger.info("Input content sanitized for session %s", session_id)

        # 处理附件（通用媒体类型，不限于图片）
        attachments = getattr(message, "attachments", None) or []
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
                logger = logging.getLogger(__name__)
                logger.warning("Attachment processing failed for session %s: %s", session_id, e)

        # 命令拦截
        if self._command_interceptor is not None:
            try:
                handle_async = getattr(self._command_interceptor, "handle_async", None)
                if handle_async is not None:
                    intercept_result = await handle_async(
                        InputMessage(content=sanitized_content, session_id=session_id, metadata=message.metadata)
                    )
                else:
                    intercept_result = self._command_interceptor.handle(
                        InputMessage(content=sanitized_content, session_id=session_id, metadata=message.metadata)
                    )
            except Exception:
                logger = logging.getLogger(__name__)
                logger.exception("CommandInterceptor failed for session %s", session_id)
                intercept_result = None
            if intercept_result is not None:
                return AgentResult(content=intercept_result, stop_reason="command_intercepted")

        try:
            # 1. 加载上下文状态
            context_state = await self._context_manager.load_with_metadata(
                session_id,
                metadata={"input_metadata": runtime_info or {}},
            )

            # 1.5 崩溃恢复：若存在 checkpoint，修复不完整状态并重新加载
            load_checkpoint = getattr(self._context_manager, "load_checkpoint", None)
            if load_checkpoint is not None:
                recovered = await load_checkpoint(session_id)
                if recovered:
                    recovered = self._sanitize_recovered_messages(recovered)
                    # MemorySystemContextManager.save() 不保存 assistant_result.messages，
                    # 因此 recovered 消息必须通过 memory_system.add_messages() 直接写入
                    memory_system = getattr(self._context_manager, "memory_system", None)
                    if memory_system is not None:
                        from framework.memory.core.scope import MemoryContext

                        ctx = self._context_manager._context_cache.get(session_id)
                        if ctx is None:
                            ctx = MemoryContext(
                                session_id=session_id,
                                user_id=getattr(
                                    self._context_manager, "default_user_id", "default"
                                ),
                            )
                        await memory_system.add_messages(ctx, recovered)
                    clear_checkpoint = getattr(self._context_manager, "clear_checkpoint", None)
                    if clear_checkpoint is not None:
                        await clear_checkpoint(session_id)
                    context_state = await self._context_manager.load_with_metadata(
                        session_id,
                        metadata={"input_metadata": runtime_info or {}},
                    )

            # 2. 构建用户消息（如有媒体附件，构建多模态 content）
            if media_blocks and _media_processor is not None:
                try:
                    multimodal_content = _media_processor.build_content(
                        sanitized_content, media_blocks
                    )
                except Exception:
                    multimodal_content = sanitized_content
            else:
                multimodal_content = sanitized_content

            user_message = {
                "role": "user",
                "content": multimodal_content,
            }

            # 3. 预先保存用户消息（避免长时间 ReAct 循环中记忆缺失）
            await self._context_manager.save(
                session_id=session_id,
                user_message=user_message,
                assistant_result=AgentResult(),
                metadata={
                    "input_metadata": runtime_info or {},
                    "finish_reason": "in_progress",
                },
            )

            # 4. 重新加载以获取包含用户消息的最新历史
            context_state = await self._context_manager.load_with_metadata(
                session_id,
                metadata={"input_metadata": runtime_info or {}},
            )

            # 4.5 恢复当前用户消息的完整多模态内容
            #（memory 中保存的是 sanitize 后的占位符，LLM 需要看到完整媒体）
            if media_blocks and _media_processor is not None:
                logger = logging.getLogger(__name__)
                pending = await restore_multimodal_in_history(
                    context_state.history, multimodal_content, logger
                )
                if pending is not None:
                    context_state.history = ListMessageHistory(pending)

            # 5. 构建系统提示词（注入 tools / skills / runtime info，必须在 load() 之后，
            #    否则 load() 会覆盖已注入 tools/skills 的 system_prompt）
            build_runtime_info = dict(runtime_info) if runtime_info else {}
            if "caller_context" not in build_runtime_info:
                agent_name = (
                    self._agent_descriptor.address.name
                    if self._agent_descriptor
                    else "main"
                )
                build_runtime_info["caller_context"] = {"agent_name": agent_name}
            system_prompt = await self._context_manager.build_system_prompt(
                tool_manager=self._tool_manager,
                skill_manager=self._skill_manager,
                runtime_info=build_runtime_info,
            )
            context_state.system_prompt = system_prompt

            # 6. 构建 AgentContext（确保当前用户消息在 history 中）
            # 使用 MultiAgentContextBuilder 构建上下文（如果配置）
            if self._context_builder is not None and self._agent_descriptor is not None:
                from ..multi_agent.address import AgentAddress
                from ..multi_agent.envelope import AgentMessageEnvelope
                envelope = AgentMessageEnvelope(
                    payload={"content": sanitized_content},
                    source=AgentAddress(kind="user", name=message.sender_id or "unknown"),
                    target=AgentAddress(kind="agent", name=route_result.agent_name if route_result else "main"),
                    message_type=route_result.envelope_metadata.get("message_type", "agent_message") if route_result else "agent_message",
                    conversation_id=route_result.conversation_id if route_result else session_id,
                    agent_session_id=session_id,
                    metadata=message.metadata or {},
                )
                base_history = await history_to_list(context_state.history)
                if base_history and base_history[-1].get("role") == "user":
                    base_history = base_history[:-1]
                built_messages = self._context_builder.build_messages(
                    history=base_history,
                    current_envelope=envelope,
                    agent_descriptor=self._agent_descriptor,
                )
                system_msgs = [m for m in built_messages if m.get("role") == "system"]
                if system_msgs:
                    context_state.system_prompt = "\n\n".join(m.get("content", "") for m in system_msgs)
                non_system = [m for m in built_messages if m.get("role") != "system"]
                # Write built non-system messages back into the underlying MessageHistory
                if isinstance(context_state.history, MessageHistory) and not isinstance(
                    context_state.history, ListMessageHistory
                ):
                    if non_system:
                        await context_state.history.replace_all(non_system)
                    else:
                        await context_state.history.clear()
                else:
                    context_state.history = ListMessageHistory(non_system)

            async def on_checkpoint(msgs: list[dict[str, Any]]) -> None:
                save_checkpoint = getattr(self._context_manager, "save_checkpoint", None)
                if save_checkpoint is not None:
                    await save_checkpoint(session_id, msgs)

            agent_name = self._agent_descriptor.address.name if self._agent_descriptor else "main"

            agent_context = AgentContext(
                system_prompt=context_state.system_prompt,
                history=context_state.history,
                tool_manager=self._tool_manager,
                session_id=session_id,
                max_iterations=getattr(self._agent, "max_iterations", 10),
                temperature=getattr(message, "metadata", {}).get("temperature"),
                max_tokens=getattr(message, "metadata", {}).get("max_tokens"),
                metadata={"session_id": session_id, "agent_name": agent_name},
                extensions={
                    ExtensionKey.RUNTIME_CTX_MGR: self._runtime_context_manager,
                    ExtensionKey.ON_CHECKPOINT: on_checkpoint,
                },
            )

            # Build ReActRuntime via framework RuntimeAssembler
            if self._hook_runner or self._interceptor_chain or self._checkpoint_store:
                from framework.agents.react.assembler import RuntimeAssembler, RuntimeServicesConfig

                agent_context.runtime = await RuntimeAssembler.assemble(RuntimeServicesConfig(
                    mode="full",
                    hooks=self._hook_runner,
                    interceptors=list(self._interceptor_chain.interceptors) if self._interceptor_chain else None,
                    checkpoint_store=self._checkpoint_store,
                ))

            # 5.5 设置当前 conversation_id 上下文变量（供 peer 通信工具使用）
            from ..multi_agent.session_id import DefaultSessionIdStrategy
            from ..multi_agent.subagent_manager import current_conversation_id
            raw_id = runtime_info.get("conversation_id", session_id) if runtime_info else session_id
            conversation_id, _agent_name = DefaultSessionIdStrategy().parse(raw_id)
            conv_token = current_conversation_id.set(conversation_id)

            try:
                # 6. 执行 Agent
                result = await self._agent.run(
                    context=agent_context,
                    emitter=emitter,
                )
            finally:
                current_conversation_id.reset(conv_token)

            # 6.5 为最后一条 assistant 消息注入 attachments metadata（与 Pipeline 保持一致）
            if result and result.attachments:
                await inject_attachments_to_history(
                    context_state.history, result.attachments
                )

            # 6.6 保存 assistant 结果到上下文
            await self._context_manager.save(
                session_id=session_id,
                user_message=None,
                assistant_result=result,
                metadata={
                    "input_metadata": runtime_info or {},
                    "finish_reason": result.stop_reason,
                },
            )

            # 7. Turn 结束，显式 flush Working Memory 到 Short-term Memory
            await self._context_manager.flush(session_id)

            # 8. 尝试触发 DreamEngine 整理长期记忆
            await self._maybe_trigger_dream(session_id)

            return result

        except GraphInterrupt:
            # Approval interrupt must propagate to the caller,
            # not be treated as a generic execution error.
            raise
        except Exception as e:
            # 错误处理：通过 emitter 发送错误事件
            await emitter.emit_error(str(e))
            return AgentResult(
                content="",
                stop_reason="error",
                error=str(e),
            )
        finally:
            pass

    @staticmethod
    def _sanitize_recovered_messages(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """修复恢复消息中不完整的 tool-call 链。

        如果最后一条 assistant 消息包含 tool_calls 但没有对应的 tool 结果，
        则追加错误占位 tool 结果，避免 LLM API 报错。
        """
        if not messages:
            return messages

        result = list(messages)
        completed_ids = {
            msg["tool_call_id"]
            for msg in result
            if msg.get("role") == "tool" and msg.get("tool_call_id")
        }

        for msg in result:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    tc_id = tc.get("id")
                    if tc_id and tc_id not in completed_ids:
                        result.append(
                            {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "name": tc.get("function", {}).get("name", "unknown"),
                                "content": "Error: Task interrupted before this tool finished.",
                            }
                        )
        return result

    async def _maybe_trigger_dream(self, session_id: str) -> None:
        """当未处理历史条目超过阈值时，后台触发 DreamEngine。"""
        if self._dream_engine is None:
            return
        dream_engine = self._dream_engine
        memory_system = getattr(self._context_manager, "memory_system", None)
        if memory_system is None:
            return
        ctx = getattr(self._context_manager, "_context_cache", {}).get(session_id)
        if ctx is None:
            ctx = MemoryContext(session_id=session_id)
        try:
            count = await memory_system.get_unprocessed_history_count(ctx)
        except Exception:
            return
        if count < self._dream_threshold:
            return

        scope_key = f"{ctx.session_id or ''}:{ctx.user_id or ''}:{ctx.tenant_id or ''}"
        lock = _dream_locks.setdefault(scope_key, asyncio.Lock())

        async def _run_dream(
            c: MemoryContext = ctx,
            engine: Any = dream_engine,
            lk: asyncio.Lock = lock,
        ) -> None:
            async with lk:
                try:
                    await engine.run(c)
                except Exception as dream_err:
                    import logging

                    logging.getLogger(__name__).warning("DreamEngine failed: %s", dream_err)

        asyncio.create_task(_run_dream())

    async def clear_session(self, session_id: str) -> None:
        """清空指定会话

        Args:
            session_id: 会话 ID
        """
        await self._context_manager.clear(session_id)

    async def startup(self) -> None:
        """启动 Session，初始化所有管理器"""
        await self._tool_manager.startup()

    async def shutdown(self) -> None:
        """关闭 Session，释放资源"""
        await self._tool_manager.shutdown()

    @property
    def agent(self) -> Agent[E]:
        """获取 Agent 实例"""
        return self._agent

    @property
    def tool_manager(self) -> ToolManager:
        """获取 ToolManager 实例"""
        return self._tool_manager

    @property
    def context_manager(self) -> ContextManager:
        """获取 ContextManager 实例"""
        return self._context_manager



