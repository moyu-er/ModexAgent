"""ReActAgent 实现

提供 ReActEvent 枚举和 ReActAgent 类，实现 Thought → Action → Observation 循环。
"""

import asyncio
import json
import logging
from enum import Enum
from typing import Any

from ...core.agent import Agent, AgentContext, current_agent_context
from ...core.constants import DefaultValues, FinishReason
from ...core.emitter import AgentResult, ContentEmitter, ToolCall
from ...core.events import AgentEvent
from ...core.hooks import AgentRunHook
from ...core.provider import LLMProvider, StreamingLLMProvider
from ...core.tool_manager import ToolResult
from ...core.types import LLMResponse

logger = logging.getLogger(__name__)

# P0-a: 合理默认值
_HOOK_TIMEOUT = 10.0
_TOOL_TIMEOUT = DefaultValues.TOOL_TIMEOUT_SECONDS


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
    ):
        self.provider = provider
        self._hook_timeout = hook_timeout
        self._tool_timeout = tool_timeout

    @property
    def name(self) -> str:
        return "ReActAgent"

    async def run(
        self,
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
    ) -> AgentResult:
        """执行 ReAct 循环

        Args:
            context: Agent 执行上下文
            emitter: 内容发射器

        Returns:
            AgentResult: 执行结果
        """
        # 每轮开始时清空 attachments，避免跨轮污染
        context.attachments = []
        # 将当前 AgentContext 绑定到 contextvar，供 Tool 内部访问
        ctx_token = current_agent_context.set(context)

        iteration = 0

        await emitter.emit(ReActEvent.START)

        all_new_messages: list[dict[str, Any]] = []
        result = AgentResult(content="", stop_reason="error")

        try:
            await self._call_hooks("before_turn", context)

            while iteration < context.max_iterations:
                iteration += 1
                await emitter.emit(ReActEvent.ITERATION_START, {"iteration": iteration})

                await self._call_hooks("before_iteration", context)

                # 重新构建 messages，以便 Hook 注入的新消息被纳入上下文
                messages = await context.to_messages()

                # 应用上下文治理（token 预算、tool 链修复等）
                if context.governance is not None:
                    messages = await context.governance.apply(messages)

                response = await self._request_llm(messages, context, emitter)
                await self._call_hooks("after_llm_response", context, response)

                # 4.1: provider error response → fail turn, don't use as normal content
                if response.finish_reason == FinishReason.ERROR.value:
                    error_text = response.error or response.content or "LLM request failed"
                    logger.warning(
                        "ReActAgent turn failed: finish_reason=%s error=%s",
                        response.finish_reason,
                        (error_text or "")[:200],
                    )
                    await emitter.emit(ReActEvent.ERROR, error_text)
                    result = AgentResult(
                        error=error_text,
                        stop_reason="error",
                        messages=all_new_messages,
                        attachments=context.attachments,
                    )
                    await emitter.emit_complete(result)
                    return result

                content = response.content or ""
                reasoning = response.reasoning_content
                tool_calls = response.tool_calls

                # 限制每轮最大工具调用数
                max_tools = context.max_tools_per_turn
                if max_tools is not None and tool_calls and len(tool_calls) > max_tools:
                    error_msg = f"Exceeded max_tools_per_turn limit ({max_tools})"
                    logger.warning(error_msg)
                    await emitter.emit(ReActEvent.ERROR, error_msg)
                    result = AgentResult(
                        content=error_msg,
                        stop_reason="error",
                        messages=all_new_messages,
                        attachments=context.attachments,
                    )
                    await emitter.emit_complete(result)
                    return result

                # 本轮新增的消息列表（用于 after_tool_execution hook 参数）
                iteration_messages: list[dict[str, Any]] = []

                assistant_message = self._build_assistant_message(content, tool_calls)
                messages.append(assistant_message)
                await context.history.append(assistant_message)
                all_new_messages.append(assistant_message)
                iteration_messages.append(assistant_message)
                await self._save_checkpoint(all_new_messages, context)

                if tool_calls:
                    progress_hint = self._format_tool_hint(tool_calls)
                    await emitter.emit(ReActEvent.PROGRESS, {"hint": progress_hint, "tool_hint": True})

                    await self._call_hooks("before_tool_execution", context, tool_calls)

                    for tool_call in tool_calls:
                        await emitter.emit(ReActEvent.TOOL_CALL_START, tool_call)

                        result = await self._execute_tool(tool_call, context)

                        await emitter.emit(ReActEvent.TOOL_CALL_END, (tool_call, result))

                        tool_message = self._build_tool_message(result, tool_call.call_id)
                        messages.append(tool_message)
                        await context.history.append(tool_message)
                        all_new_messages.append(tool_message)
                        iteration_messages.append(tool_message)
                        await self._save_checkpoint(all_new_messages, context)

                    await self._call_hooks(
                        "after_tool_execution",
                        context,
                        [msg for msg in iteration_messages if msg.get("role") == "tool"],
                    )

                    await self._call_hooks("after_iteration", context)
                    await emitter.emit(
                        ReActEvent.ITERATION_END,
                        {"iteration": iteration, "has_tool_calls": True}
                    )
                else:
                    result = AgentResult(
                        content=content,
                        reasoning=reasoning,
                        messages=all_new_messages,
                        attachments=context.attachments,
                    )
                    await self._clear_checkpoint(context)
                    await emitter.emit(ReActEvent.FINAL_OUTPUT, result)
                    await emitter.emit_complete(result)
                    return result

            result = AgentResult(
                content="达到最大迭代次数",
                stop_reason="max_iterations",
                messages=all_new_messages,
                attachments=context.attachments,
            )
            await self._clear_checkpoint(context)
            await emitter.emit(ReActEvent.MAX_ITERATIONS, result)
            await emitter.emit_complete(result)
            return result

        except asyncio.CancelledError:
            logger.warning(
                "ReActAgent cancelled (iteration=%d, messages=%d)",
                iteration,
                len(all_new_messages),
            )
            try:
                await asyncio.shield(self._save_checkpoint(all_new_messages, context))
            except Exception:
                logger.warning("Checkpoint save failed during cancellation", exc_info=True)
            result = AgentResult(
                error="Agent cancelled",
                stop_reason=FinishReason.CANCELLED.value,
                messages=all_new_messages,
                attachments=context.attachments,
            )
            raise
        except Exception as e:
            logger.exception("Agent execution error")
            await emitter.emit(ReActEvent.ERROR, str(e))
            await self._save_checkpoint(all_new_messages, context)
            result = AgentResult(
                error=str(e), stop_reason="error", messages=all_new_messages,
                attachments=context.attachments,
            )
            await emitter.emit_complete(result)
            return result
        finally:
            current_agent_context.reset(ctx_token)
            await self._call_hooks("after_turn", context, result)

    async def _save_checkpoint(
        self,
        all_new_messages: list[dict[str, Any]],
        context: AgentContext,
    ) -> None:
        """保存检查点。"""
        if context.on_checkpoint:
            await context.on_checkpoint(list(all_new_messages))

    async def _clear_checkpoint(self, context: AgentContext) -> None:
        """清空检查点。"""
        if context.on_checkpoint:
            await context.on_checkpoint([])

    async def _call_hooks(
        self,
        method_name: str,
        context: AgentContext,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """调用 AgentContext 中注册的所有 hooks 的指定方法，每个 hook 带独立 timeout。"""
        for hook in context.hooks or []:
            if hook is None or not isinstance(hook, AgentRunHook):
                continue
            method = getattr(hook, method_name, None)
            if method is None:
                continue
            try:
                await asyncio.wait_for(
                    method(context, *args, **kwargs),
                    timeout=self._hook_timeout,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.warning(
                    "Hook %s.%s timed out after %.1fs",
                    type(hook).__name__,
                    method_name,
                    self._hook_timeout,
                )
            except Exception:
                logger.exception("Hook %s failed in %s", type(hook).__name__, method_name)

    async def _request_llm(
        self,
        messages: list[dict[str, Any]],
        context: AgentContext,
        emitter: ContentEmitter[ReActEvent],
    ) -> LLMResponse:
        """请求 LLM，根据 emitter 偏好选择流式或非流式路径。"""
        wants_streaming = emitter.wants_streaming()
        is_streaming_provider = isinstance(self.provider, StreamingLLMProvider)

        if wants_streaming and is_streaming_provider:

            async def _on_content_delta(delta: str) -> None:
                if delta:
                    await emitter.emit_delta(delta)
                    await emitter.emit(ReActEvent.MODEL_OUTPUT, delta)

            async def _on_reasoning_delta(delta: str) -> None:
                if delta:
                    await emitter.emit(ReActEvent.MODEL_REASONING, delta)

            assert isinstance(self.provider, StreamingLLMProvider)
            response = await self.provider.chat_stream(
                messages=messages,
                tools=context.get_tool_descriptions() if context.tool_manager else None,
                temperature=context.temperature or 0.7,
                max_tokens=context.max_tokens,
                on_content_delta=_on_content_delta,
                on_reasoning_delta=_on_reasoning_delta,
            )
            await emitter.emit_stream_end(resuming=bool(response.tool_calls))
            return response
        else:
            response = await self.provider.chat(
                messages=messages,
                tools=context.get_tool_descriptions() if context.tool_manager else None,
                temperature=context.temperature or 0.7,
                max_tokens=context.max_tokens,
            )
            if response.content:
                await emitter.emit_content(response.content)
                await emitter.emit(ReActEvent.MODEL_OUTPUT, response.content)
            if response.reasoning_content:
                await emitter.emit(ReActEvent.MODEL_REASONING, response.reasoning_content)
            await emitter.emit_stream_end(resuming=bool(response.tool_calls))
            return response

    async def _execute_tool(
        self,
        tool_call: ToolCall,
        context: AgentContext,
    ) -> ToolResult:
        """执行工具（使用 ToolManager），带独立 timeout。"""
        try:
            result = await asyncio.wait_for(
                context.tool_manager.execute(
                    tool_call.tool_name,
                    tool_call.arguments or {},
                ),
                timeout=self._tool_timeout,
            )
            return result
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning(
                "Tool %s timed out after %.1fs",
                tool_call.tool_name,
                self._tool_timeout,
            )
            return ToolResult(
                tool_name=tool_call.tool_name,
                result=None,
                error=f"Error: Tool execution timeout after {self._tool_timeout:.0f}s",
            )
        except Exception as e:
            logger.warning("Tool %s execution failed: %s", tool_call.tool_name, e)
            return ToolResult(
                tool_name=tool_call.tool_name,
                result=None,
                error=f"Error: {e}",
            )

    @staticmethod
    def _format_tool_hint(tool_calls: list[ToolCall]) -> str:
        """格式化工具调用提示。"""
        if not tool_calls:
            return "准备执行工具..."
        if len(tool_calls) == 1:
            return f"正在调用 {tool_calls[0].tool_name}..."
        names = ", ".join(tc.tool_name for tc in tool_calls)
        return f"正在调用工具: {names}..."

    def _build_assistant_message(
        self,
        content: str,
        tool_calls: list[ToolCall],
    ) -> dict[str, Any]:
        """构建 assistant 消息

        注意：当只有 tool_calls 没有 content 时，OpenAI API 要求 content 为 null 而非空字符串
        """
        message_content = None if not content and tool_calls else content or ""

        message: dict[str, Any] = {"role": "assistant", "content": message_content}
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": tc.call_id or f"call_{i}",
                    "type": "function",
                    "function": {
                        "name": tc.tool_name,
                        "arguments": json.dumps(tc.arguments) if tc.arguments else "{}",
                    },
                }
                for i, tc in enumerate(tool_calls)
            ]
        return message

    _MAX_TOOL_RESULT_CHARS = 20000  # 约 5000 tokens

    def _build_tool_message(self, result: ToolResult, call_id: str | None = None) -> dict[str, Any]:
        """构建 tool 消息

        注意：tool content 不能为空，至少要有空格或错误信息
        """
        if result.error:
            content = f"Error: {result.error}"
        elif result.result is not None:
            content = str(result.result)
        else:
            content = " "

        if not content.strip():
            content = " "

        if len(content) > self._MAX_TOOL_RESULT_CHARS:
            content = content[:self._MAX_TOOL_RESULT_CHARS] + (
                f"\n... (truncated, {len(content)} chars total)"
            )

        tool_call_id = call_id or result.tool_name

        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": result.tool_name,
            "content": content,
        }
