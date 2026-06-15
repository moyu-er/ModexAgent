"""ReActAgent 实现

提供 ReActEvent 枚举和 ReActAgent 类，实现 Thought → Action → Observation 循环。
"""

import asyncio
import contextlib
import logging
from enum import Enum
from typing import Any, Literal

from framework.agents.react.state import get_react_state
from framework.control.exceptions import AgentControlError
from framework.hook import HookPayload, HookPoint
from framework.interceptor.abc import (
    LLMStreamChunk,
    LLMStreamContext,
    ToolCallContext,
)
from framework.runtime.enums import TurnCustomKey, TurnPhase

from ...core.agent import Agent, AgentContext, current_agent_context
from ...core.constants import DefaultValues, StopReason
from ...core.emitter import AgentResult, ContentEmitter
from ...core.events import AgentEvent
from ...core.provider import LLMProvider, StreamingLLMProvider
from ...core.tool_manager import ToolResult
from ...core.types import LLMResponse, ToolCall

logger = logging.getLogger(__name__)

# P0-a: 合理默认值
_HOOK_TIMEOUT = 10.0
_TOOL_TIMEOUT = DefaultValues.TOOL_TIMEOUT_SECONDS

# Injection drain limits (ref: nanobot design)
_MAX_INJECTIONS_PER_PHASE = 3
_MAX_INJECTION_CYCLES = 5


class ReActEvent(AgentEvent, Enum):
    """ReActAgent 特有的事件类型

    说明：
    - MODEL_OUTPUT: 模型生成的最终文本输出（流式片段）。
      **注意**：在 ReAct 流式输出过程中，无法预知这是否是最终结果，
      因为模型可能在输出后决定调用工具。
      外部应根据后续是否有 TOOL_CALL 事件来判断。

    - MODEL_REASONING: 模型的推理/思考过程（新增，DeepSeek R1、Kimi 等模型）。
      与 MODEL_OUTPUT 分开，业务层决定如何展示。

    - TOOL_CALL_START: 准备调用工具
    - TOOL_CALL_END: 工具调用完成（包含结果）
    - ITERATION_START/END: 单次 Thought-Action-Observation 循环
    - FINAL_OUTPUT: 确定是最终输出（无后续工具调用）
    """

    # 模型输出（流式，最终输出内容）
    MODEL_OUTPUT = "model_output"

    # 模型推理/思考过程（DeepSeek R1, Kimi 等模型）
    MODEL_REASONING = "model_reasoning"

    # 工具相关
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"

    # 执行状态
    ITERATION_START = "iteration_start"
    ITERATION_END = "iteration_end"

    # 最终结果（确定无后续工具调用）
    FINAL_OUTPUT = "final_output"

    # 生命周期（可选，如果需要）
    START = "start"
    ERROR = "error"
    MAX_ITERATIONS = "max_iterations"
    PROGRESS = "progress"


def _get_turn_messages(ctx: AgentContext) -> list[dict[str, Any]]:
    """Extract current-turn messages from typed state or metadata fallback."""
    from framework.agents.react.state import get_react_state as _grs

    state = _grs(ctx)
    if state is not None:
        return [
            md.message.to_dict() if hasattr(md.message, "to_dict") else md.message
            for md in state.message_delta
        ]
    return []


class ReActAgent(Agent[ReActEvent]):
    """ReAct 推理模式实现

    执行 Thought → Action → Observation 循环。
    所有输出通过 ContentEmitter。

    触发的事件（由 ReActEvent 枚举定义）：
    - MODEL_OUTPUT: LLM 生成的文本内容（流式）
    - TOOL_CALL_START: 准备调用工具
    - TOOL_CALL_END: 工具执行结果
    - ITERATION_START/ITERATION_END: 每次 ReAct 迭代的开始和结束
    - FINAL_OUTPUT: 确认的最终回复（无后续工具调用）
    - ERROR: 发生错误
    - MAX_ITERATIONS: 达到最大迭代次数

    事件流示例（单次工具调用）：
    1. MODEL_OUTPUT (流式) -> "让我查一下天气..."
    2. TOOL_CALL_START -> {"tool": "weather", "args": {"city": "北京"}}
    3. TOOL_CALL_END -> {"tool": "weather", "result": "晴天 25°C"}
    4. ITERATION_END
    5. MODEL_OUTPUT (流式) -> "北京的天气是..."
    6. FINAL_OUTPUT -> "北京的天气是晴天，25°C"
    """

    # 该 Agent 使用的事件类型枚举
    event_enum = ReActEvent

    def __init__(
        self,
        provider: LLMProvider,
        hook_timeout: float = _HOOK_TIMEOUT,
        tool_timeout: float = _TOOL_TIMEOUT,
        *,
        mode: Literal["clean", "full"] = "full",
    ) -> None:
        from framework.agents.react.graph import ReActGraph
        from framework.core.graph.engine import GraphEngine

        self.provider = provider
        self._hook_timeout = hook_timeout
        self._tool_timeout = tool_timeout
        self.mode = mode
        self.graph = ReActGraph(self, mode=mode)
        self.engine = GraphEngine(self.graph)

    @property
    def name(self) -> str:
        return "ReActAgent"

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
    ) -> AgentResult:
        """Delegate to GraphEngine (thin shell).

        Args:
            context: Agent execution context
            emitter: content emitter

        Returns:
            AgentResult: execution result
        """
        from framework.core.graph.interrupt import GraphInterrupt

        # 每轮开始时清空 attachments，避免跨轮污染
        context.attachments = []
        context.emitter = emitter

        # Use prebuilt runtime if already set on context; otherwise build clean runtime.
        if context.runtime is None:
            from framework.agents.react.state import ReActTurnState
            from framework.runtime.enums import AgentKind
            from framework.runtime.models import TurnIdentity
            from framework.runtime.services import AgentRuntime, AgentRuntimeServices

            state = ReActTurnState(
                identity=context.identity
                or TurnIdentity(agent_id="react", session=context.session, turn_id="default"),
                agent_kind=AgentKind.REACT,
                phase=TurnPhase.CREATED,
            )
            context.identity = state.identity
            context.runtime = AgentRuntime(services=AgentRuntimeServices(), state=state)
        runtime = context.runtime
        ctx_token = current_agent_context.set(context)

        result = AgentResult(content="", stop_reason=StopReason.ERROR)

        async def actual_turn():
            nonlocal result
            if runtime.hooks:
                await runtime.hooks.dispatch(HookPoint.BEFORE_TURN, context)

            # Drain control commands before starting turn
            if context.runtime and context.runtime.control_channel:
                from framework.hook.builtin.control_drain import drain_control_channel

                await drain_control_channel(
                    context.runtime.control_channel,
                    context,
                    turn_uuid=context.runtime.turn_uuid,
                )

            result = await self.engine.run(context)
            if runtime.hooks:
                await runtime.hooks.dispatch(
                    HookPoint.AFTER_TURN,
                    context,
                    HookPayload(data={"result": result}),
                )
            return result

        try:
            if runtime.interceptors is not None:
                from framework.interceptor.abc import InterceptorScope

                if runtime.interceptors.has_scope(InterceptorScope.TURN):
                    result = await runtime.interceptors.around_turn(context, actual_turn)
                else:
                    result = await actual_turn()
            else:
                result = await actual_turn()
            return result
        except GraphInterrupt:
            raise
        except AgentControlError as e:
            logger.info(
                "ReActAgent control exit: %s",
                str(e) or "error",
            )
            raise
        except asyncio.CancelledError:
            logger.warning("ReActAgent cancelled")
            raise
        except Exception as e:
            logger.exception("Agent execution error")
            await emitter.emit(ReActEvent.ERROR, str(e))
            all_new = _get_turn_messages(context)
            result = AgentResult(
                error=str(e),
                stop_reason=StopReason.ERROR,
                messages=all_new,
                attachments=context.attachments,
            )
            await emitter.emit_complete(result)
            return result
        finally:
            # FINALLY_TURN: fires regardless of success/error/cancel.
            # SubagentAutoSendHook and cleanup hooks always execute.
            if runtime.hooks:
                try:
                    await runtime.hooks.dispatch(
                        HookPoint.FINALLY_TURN,
                        context,
                        HookPayload(data={"result": result}),
                    )
                except Exception:
                    logger.exception("FINALLY_TURN hook dispatch failed")
            # Clean up typed state
            state = get_react_state(context)
            if state is not None:
                state.phase = (
                    TurnPhase.COMPLETED
                    if state.phase not in (TurnPhase.COMPLETED, TurnPhase.FAILED)
                    else state.phase
                )
            context.emitter = None
            current_agent_context.reset(ctx_token)

    def _resolve_hook_timeout(self, context: AgentContext) -> float:
        """从 runtime.safety 读取 hook_timeout，带 fallback。"""
        safety = context.runtime.safety if context.runtime else None
        if safety is not None:
            return safety.turn.hook_timeout_seconds
        return self._hook_timeout

    def _resolve_tool_timeout(self, context: AgentContext) -> float:
        """从 runtime.safety 读取 tool_timeout，带 fallback。"""
        safety = context.runtime.safety if context.runtime else None
        if safety is not None:
            return safety.turn.tool_timeout_seconds
        return self._tool_timeout

    async def _call_hooks(
        self,
        hook_point: HookPoint,
        context: AgentContext,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Dispatch hook via context.runtime (set by ReActAgent.run()).

        Used by LLMNode and ToolNode during graph execution.
        """
        if context.runtime is None or context.runtime.hooks is None:
            return

        payload_data: dict[str, Any] = {}
        method_name = hook_point.value
        if args:
            if method_name == "after_turn":
                payload_data = {"result": args[0]} if args else {}
            elif method_name == "after_llm_response":
                payload_data = {"response": args[0]} if args else {}
            elif method_name in ("before_tool_execution", "after_tool_execution"):
                if method_name == "before_tool_execution":
                    payload_data = {"tool_calls": args[0]}
                else:
                    payload_data = {"results": args[0]}

        await context.runtime.hooks.dispatch(
            hook_point,
            context,
            HookPayload(data=payload_data),
            hook_timeout=self._resolve_hook_timeout(context),
        )

    async def _execute_tool(
        self,
        tool_call: ToolCall,
        context: AgentContext,
    ) -> ToolResult:
        """执行工具，优先使用 InterceptorChain 包裹。"""
        interceptor_chain = context.runtime.interceptors if context.runtime else None
        if interceptor_chain is not None:
            call_ctx = ToolCallContext(
                tool_call=tool_call,
                tool_name=tool_call.tool_name,
                arguments=tool_call.arguments or {},
                session_id=str(context.session),
            )

            async def _actual() -> ToolResult:
                return await self._execute_tool_raw(tool_call, context)

            return await interceptor_chain.around_tool_call(
                context,
                call_ctx,
                _actual,
            )

        return await self._execute_tool_raw(tool_call, context)

    async def _execute_tool_raw(
        self,
        tool_call: ToolCall,
        context: AgentContext,
    ) -> ToolResult:
        """执行工具（使用 ToolManager），带独立 timeout。"""
        tool_timeout = self._resolve_tool_timeout(context)
        try:
            result = await asyncio.wait_for(
                context.tool_manager.execute(
                    tool_call.tool_name,
                    tool_call.arguments or {},
                ),
                timeout=tool_timeout,
            )
            return result
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning(
                "Tool %s timed out after %.1fs",
                tool_call.tool_name,
                tool_timeout,
            )
            return ToolResult(
                tool_name=tool_call.tool_name,
                result=None,
                error=f"Error: Tool execution timeout after {tool_timeout:.0f}s",
            )
        except Exception as e:
            logger.warning("Tool %s execution failed: %s", tool_call.tool_name, e)
            return ToolResult(
                tool_name=tool_call.tool_name,
                result=None,
                error=f"Error: {e}",
            )

    async def _stream_with_control(
        self,
        messages: list[dict[str, Any]],
        context: AgentContext,
    ) -> LLMResponse:
        """通过 InterceptorChain 包裹的 LLM 流式调用。"""
        assert isinstance(self.provider, StreamingLLMProvider)
        emitter = context.emitter

        stream_ctx = LLMStreamContext(
            messages=messages,
            model=getattr(self.provider, "model", None),
            session_id=str(context.session),
        )

        accumulated_content = ""
        accumulated_reasoning = ""
        finish_reason = "stop"
        tool_calls_list: list[ToolCall] = []

        async def _actual_stream():
            """实际调用 provider.chat_stream，将 chunk 转为 LLMStreamChunk。"""
            nonlocal tool_calls_list

            async def _on_content_delta(delta: str) -> None:
                if delta:
                    await emitter.emit_delta(delta)
                    await emitter.emit(ReActEvent.MODEL_OUTPUT, delta)

            async def _on_reasoning_delta(delta: str) -> None:
                if delta:
                    await emitter.emit(ReActEvent.MODEL_REASONING, delta)

            response = await self.provider.chat_stream(
                messages=messages,
                tools=context.get_tool_descriptions() if context.tool_manager else None,
                temperature=context.temperature or 0.7,
                max_tokens=context.max_tokens,
                on_content_delta=_on_content_delta,
                on_reasoning_delta=_on_reasoning_delta,
            )
            tool_calls_list = list(response.tool_calls or [])
            yield LLMStreamChunk(
                content_delta=response.content,
                reasoning_delta=response.reasoning_content,
                finish_reason=response.finish_reason,
            )

        interceptor_chain = context.runtime.interceptors if context.runtime else None
        async for chunk in interceptor_chain.around_llm_stream(
            context,
            stream_ctx,
            _actual_stream,
        ):
            if chunk.control_action == "cancel":
                finish_reason = chunk.finish_reason or "cancelled"
                logger.warning(
                    "LLM stream cancelled session=%s finish_reason=%s",
                    str(context.session),
                    finish_reason,
                )
                break
            if chunk.content_delta:
                accumulated_content += chunk.content_delta
            if chunk.reasoning_delta:
                accumulated_reasoning += chunk.reasoning_delta
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        has_tool_calls = bool(tool_calls_list)
        await emitter.emit_stream_end(resuming=has_tool_calls)
        return LLMResponse(
            content=accumulated_content or None,
            reasoning_content=accumulated_reasoning or None,
            finish_reason=finish_reason,
            tool_calls=tool_calls_list,
        )

    async def _drain_injections(
        self,
        context: AgentContext,
        max_per_phase: int = _MAX_INJECTIONS_PER_PHASE,
    ) -> list[str]:
        """消费注入队列中的用户消息，追加到 history。"""
        q = context.runtime.injection_queue if context.runtime else None
        if q is None:
            return []

        cycle_count: int = (
            context.runtime.state.custom.get(TurnCustomKey.INJECTION_CYCLE_COUNT, 0)
            if context.runtime
            else 0
        )
        if cycle_count >= _MAX_INJECTION_CYCLES:
            while True:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break
            return []

        injected: list[str] = []
        for _ in range(max_per_phase):
            try:
                msg: str = q.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                await context.history.append(
                    {
                        "role": "user",
                        "content": f"[Injected during execution]: {msg}",
                    }
                )
            except Exception:
                logger.warning(
                    "Failed to inject message into history, returning to queue: %s",
                    msg[:100],
                )
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(msg)
                break
            injected.append(msg)

        if injected:
            if context.runtime:
                context.runtime.state.custom[TurnCustomKey.INJECTION_CYCLE_COUNT] = cycle_count + 1

        return injected

    # Message construction helpers moved to framework.utils.message_builder
    # (build_assistant_message, build_tool_message) to keep ReActAgent focused
    # on orchestration rather than data-formatting details.
