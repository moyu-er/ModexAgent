"""
Provides ``AgentResult`` (Pydantic ``BaseModel``) and the ``ContentEmitter[E]``
generic ABC. The streaming-aware concrete emitter lives in
``modex_agent.adapters.emitter`` (B4).
"""
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from modex_agent.core.message import ChatMessage

from .constants import StopReason
from .events import AgentEvent, EmitterConfig
from .turn_events import TurnEvent

logger = logging.getLogger(__name__)


class AgentResult(BaseModel):
    """Agent 执行结果

    包含最终输出内容和可选的推理/思考过程。
    reasoning 字段用于存储 DeepSeek R1、Kimi 等模型返回的推理内容。
    messages 字段用于存储本次执行生成的所有历史消息（包括 assistant 的 tool_calls 和 tool 结果消息）。
    """

    model_config = ConfigDict(extra="forbid")

    content: str | None = None  # 最终输出内容
    reasoning: str | None = None  # 推理/思考过程（新增）
    stop_reason: StopReason = StopReason.COMPLETED
    error: str | None = None
    messages: Sequence[ChatMessage | dict[str, Any]] = Field(
        default_factory=list
    )  # 本次执行生成的历史消息
    partial_content: str | None = None  # 取消时保留的部分内容
    attachments: list[str] = Field(default_factory=list)  # 要发送给用户的附件路径列表

    @field_validator("messages", mode="before")
    @classmethod
    def _coerce_messages(cls, v: Any) -> Any:
        if isinstance(v, list):
            return [ChatMessage.from_dict(item) if isinstance(item, dict) else item for item in v]
        return v

    def __repr__(self) -> str:
        if self.error:
            return f"AgentResult(error={self.error!r}, stop_reason={self.stop_reason})"
        return f"AgentResult(content={self.content!r}, reasoning={'...' if self.reasoning else None}, stop_reason={self.stop_reason})"


E = TypeVar("E", bound=AgentEvent)


class ContentEmitter[E: AgentEvent](ABC):
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
