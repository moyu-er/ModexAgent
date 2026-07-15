"""ContentEmitter 抽象基类和实现

提供 AgentResult 数据类和 ContentEmitter 泛型抽象基类，
以及 StreamingAwareEmitter 实现。
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ..adapters.platform import StreamingMode
from modex_agent.core.message import ChatMessage
from .constants import StopReason
from .events import AgentEvent, EmitterConfig
from .turn_events import TurnEvent, TurnTextEvent

if TYPE_CHECKING:
    from ..pipeline.adapters import OutputAdapter

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Agent 执行结果

    包含最终输出内容和可选的推理/思考过程。
    reasoning 字段用于存储 DeepSeek R1、Kimi 等模型返回的推理内容。
    messages 字段用于存储本次执行生成的所有历史消息（包括 assistant 的 tool_calls 和 tool 结果消息）。
    """

    content: str | None = None  # 最终输出内容
    reasoning: str | None = None  # 推理/思考过程（新增）
    stop_reason: StopReason = StopReason.COMPLETED
    error: str | None = None
    messages: Sequence[ChatMessage | dict[str, Any]] = field(
        default_factory=list
    )  # 本次执行生成的历史消息
    partial_content: str | None = None  # 取消时保留的部分内容
    attachments: list[str] = field(default_factory=list)  # 要发送给用户的附件路径列表

    def __repr__(self) -> str:
        if self.error:
            return f"AgentResult(error={self.error!r}, stop_reason={self.stop_reason})"
        return f"AgentResult(content={self.content!r}, reasoning={'...' if self.reasoning else None}, stop_reason={self.stop_reason})"


E = TypeVar("E", bound=AgentEvent)


class ContentEmitter(ABC, Generic[E]):
    """内容发送器抽象基类

    Agent 通过此接口输出内容。实现决定：
    - 流式还是非流式
    - 发送到哪里（Bus、内存、日志等）
    - 是否缓存、转换格式
    - 哪些事件类型需要处理

    这是 Agent 与外部世界的唯一输出接口。

    泛型参数 E 是 Agent 特定的事件枚举类型，例如 ReActEvent。
    """

    def __init__(self, config: EmitterConfig | None = None) -> None:
        self.config = config or EmitterConfig()

    def wants_streaming(self) -> bool:
        """该 Emitter 是否希望 Agent 使用流式 LLM API（chat_stream）。
        默认 False，子类按需覆盖。
        """
        return False

    async def emit(self, event: E, data: Any = None) -> None:
        """通用事件发送方法

        所有业务可配置事件（MODEL_OUTPUT, MODEL_REASONING, TOOL_CALL_START,
        TOOL_CALL_END, PROGRESS, FINAL_OUTPUT, ERROR 等）均通过此方法分发。
        框架内部控制信号（emit_delta, emit_content, emit_stream_end 等）不走此通道。

        Args:
            event: 事件类型（Agent 特定的枚举值）
            data: 事件数据
        """
        event_name = event.value if isinstance(event, Enum) else str(event)
        if not self.config.is_enabled(event_name):
            return

        await self._on_event(event, data)

    async def _on_event(self, event: E, data: Any = None) -> None:
        """生命周期事件处理钩子

        子类可以覆盖此方法来实现自定义的事件记录或过滤逻辑。
        默认实现不做任何事。
        """
        pass

    async def emit_turn_event(self, event: TurnEvent) -> None:
        """Emit a provider-neutral semantic turn event when supported."""

    @abstractmethod
    async def emit_delta(self, delta: str) -> None:
        """发送流式内容片段

        Agent 生成的每个内容片段都通过此方法输出。
        实现可以立即发送（流式）或缓存（非流式）。

        Args:
            delta: 内容片段（一定是增量，不会是完整内容）
        """
        pass

    async def emit_content(self, full_content: str) -> None:
        """发送完整内容（非流式模式使用）

        默认实现回退到 emit_delta（兼容性 shim）。
        子类应该按需覆盖以区分增量和完整内容的处理。

        Args:
            full_content: 完整内容字符串
        """
        await self.emit_delta(full_content)

    async def emit_stream_end(self, resuming: bool = False) -> None:
        """当前一轮 LLM 输出已结束

        resuming=True 表示后面还有工具调用（中间态）。
        resuming=False 表示这是最终回复。
        伪流式 Emitter 可在此方法中 flush 缓冲区。

        Args:
            resuming: 是否还有后续工具调用
        """
        pass

    @abstractmethod
    async def emit_complete(self, result: AgentResult) -> None:
        """通知 Agent 执行完成

        Agent 执行完成后调用，包含最终结果。

        Args:
            result: 执行结果
        """
        pass

    @abstractmethod
    async def emit_error(self, error: str) -> None:
        """通知错误

        执行过程中发生错误时调用。

        Args:
            error: 错误信息
        """
        pass

    async def flush(self) -> None:
        """刷新缓冲区（可选）

        如果实现有缓存，此方法强制发送所有缓存内容。
        """
        pass


class StreamingAwareEmitter(ContentEmitter[E]):
    """支持流式/非流式的 Emitter 基类

    业务方应继承此类并实现具体的事件处理方法，
    决定如何处理各类事件（发送给用户、写日志、过滤等）。

    该类自动处理流式/非流式逻辑：
    - 如果 adapter 支持流式：立即转发增量
    - 如果 adapter 不支持流式：缓冲内容，最后一次性发送

    Example:
        class QQBotEmitter(StreamingAwareEmitter[ReActEvent]):
            async def emit_delta(self, delta: str) -> None:
                # 发送给用户
                await self.output_adapter.send(
                    OutputMessage(content=delta), self.session_id
                )

            async def _on_event(self, event, data) -> None:
                if event.value == "model_reasoning":
                    logger.info(f"[Reasoning] {data}")
    """

    def __init__(
        self,
        output_adapter: "OutputAdapter",
        session_id: str,
        config: EmitterConfig | None = None,
        *,
        send_timeout: float | None = None,
    ) -> None:
        super().__init__(config)
        self.output_adapter = output_adapter
        self.session_id = session_id
        self._send_timeout = send_timeout
        self._content_buffer = ""
        self._reasoning_buffer = ""

    def wants_streaming(self) -> bool:
        streaming_mode = getattr(self.output_adapter, "streaming_mode", None)
        return streaming_mode in (
            StreamingMode.NATIVE,
            StreamingMode.PSEUDO,
        )

    @property
    def is_true_streaming(self) -> bool:
        """是否是真流式（Adapter 支持 NATIVE 模式）"""
        return getattr(self.output_adapter, "streaming_mode", None) == StreamingMode.NATIVE

    async def _safe_adapter_send(self, message: Any, log_label: str = "send") -> None:
        """通过 output_adapter 发送消息，带 timeout 保护。"""
        if self._send_timeout is None:
            await self.output_adapter.send(message, self.session_id)
            return
        try:
            await asyncio.wait_for(
                self.output_adapter.send(message, self.session_id),
                timeout=self._send_timeout,
            )
        except TimeoutError:
            logger.error(
                "Output adapter %s timeout after %.1fs for session=%s (op=%s)",
                getattr(self.output_adapter, "name", "unknown"),
                self._send_timeout,
                self.session_id,
                log_label,
            )
        except Exception:
            logger.exception(
                "Output adapter %s failed for session=%s (op=%s)",
                getattr(self.output_adapter, "name", "unknown"),
                self.session_id,
                log_label,
            )

    async def emit_delta(self, delta: str) -> None:
        """处理内容片段

        真流式下立即转发；伪流式/非流式下缓冲。
        """
        if not delta:
            return

        if self.is_true_streaming:
            # 真流式：立即发送到 OutputAdapter
            await self.output_adapter.send_delta(delta, self.session_id)
        else:
            # 伪流式/非流式：缓存
            self._content_buffer += delta

    async def emit_turn_event(self, event: TurnEvent) -> None:
        """Forward canonical text while rich structured events remain opt-in."""
        match event:
            case TurnTextEvent(text=text):
                await self.emit_delta(text)
            case _:
                return

    async def emit_content(self, full_content: str) -> None:
        """处理完整内容

        与 emit_delta 分离，避免语义污染。
        完整内容直接进入缓冲区。
        """
        if full_content:
            self._content_buffer += full_content

    async def emit_stream_end(self, resuming: bool = False) -> None:
        """处理一轮 LLM 输出结束

        伪流式/非流式下 flush 缓冲区，使中间输出能立即到达用户。
        """
        if not self.is_true_streaming:
            await self._flush_buffers()

    async def emit_complete(self, result: AgentResult) -> None:
        """处理完成事件

        非流式模式下，确保残留缓冲区被发送。
        流式模式下，清理缓冲区。
        如果 result 包含 attachments，一并转发到 OutputAdapter。
        """
        if not self.is_true_streaming and self._content_buffer:
            await self._flush_buffers()
        # 转发 result 中的 attachments（即使在流式模式下也需显式发送）
        if result.attachments:
            from ..core.types import OutputMessage

            await self._safe_adapter_send(
                OutputMessage(content="", attachments=result.attachments),
                log_label="attachments",
            )
        # 清理缓冲区
        self._content_buffer = ""
        self._reasoning_buffer = ""

    async def emit_error(self, error: str) -> None:
        """处理错误事件

        默认实现：通过 OutputAdapter 发送错误消息。
        """
        from ..core.types import OutputMessage

        await self._safe_adapter_send(
            OutputMessage(content=f"Error: {error}"),
            log_label="emit_error",
        )

    async def _on_event(self, event: E, data: Any = None) -> None:
        """默认事件处理：缓存 reasoning，在 final_output 时 flush。"""
        event_name = event.value if isinstance(event, Enum) else str(event)
        if event_name == "model_reasoning":
            if isinstance(data, str):
                self._reasoning_buffer += data
        elif event_name == "final_output":
            if not self.is_true_streaming:
                await self._flush_buffers()
        elif event_name == "error":
            from ..core.types import OutputMessage

            await self._safe_adapter_send(
                OutputMessage(content=f"Error: {data}"),
                log_label="on_event_error",
            )

    async def _flush_buffers(self) -> None:
        """刷新缓冲区，发送收集的内容"""
        from ..core.types import OutputMessage

        if self._content_buffer or self._reasoning_buffer:
            await self._safe_adapter_send(
                OutputMessage(
                    content=self._content_buffer,
                    metadata={"reasoning": self._reasoning_buffer}
                    if self._reasoning_buffer
                    else {},
                ),
                log_label="flush_buffers",
            )
            self._content_buffer = ""
            self._reasoning_buffer = ""
