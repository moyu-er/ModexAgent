"""Streaming-aware emitter base — bridges ``ContentEmitter`` to ``OutputAdapter``.

``StreamingAwareEmitter`` buffers or forwards content depending on the
adapter's ``StreamingMode`` (NATIVE / PSEUDO / NONE). Moved from
``core/emitter.py`` (B4); the ``ContentEmitter`` contract stays in core.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any, TypeVar

from modex_agent.adapters.output import OutputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.emitter import AgentResult, ContentEmitter
from modex_agent.core.events import AgentEvent, EmitterConfig
from modex_agent.core.turn_events import TurnEvent, TurnTextEvent
from modex_agent.messaging.models import OutputMessage

logger = logging.getLogger(__name__)

E = TypeVar("E", bound=AgentEvent)


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
        output_adapter: OutputAdapter,
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
        return self.output_adapter.streaming_mode in (
            StreamingMode.NATIVE,
            StreamingMode.PSEUDO,
        )

    @property
    def is_true_streaming(self) -> bool:
        """是否是真流式（Adapter 支持 NATIVE 模式）"""
        return self.output_adapter.streaming_mode == StreamingMode.NATIVE

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
                self.output_adapter.name,
                self._send_timeout,
                self.session_id,
                log_label,
            )
        except Exception:
            logger.exception(
                "Output adapter %s failed for session=%s (op=%s)",
                self.output_adapter.name,
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
            await self._safe_adapter_send(
                OutputMessage(content=f"Error: {data}"),
                log_label="on_event_error",
            )

    async def _flush_buffers(self) -> None:
        """刷新缓冲区，发送收集的内容"""
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
