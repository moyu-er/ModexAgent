"""Output adapters — 输出适配器基类与内置实现

``OutputAdapter`` ABC defines the outbound transport contract (send /
send_delta / flush_deltas); ``NullOutputAdapter``, ``CLIOutputAdapter``,
and ``HTTPOutputAdapter`` are the framework-bundled implementations.
Moved from ``pipeline/adapters.py`` (B4).
"""

from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any

from modex_agent.adapters.filters import ContentFilter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.messaging.models import OutputMessage


class OutputAdapter(ABC):
    """输出适配器基类

    支持多种输出目标：QQ、CLI、HTTP Response、Webhook、日志等。
    支持真流式输出通过 send_delta() 方法。
    """

    content_filter: ContentFilter | None = None

    @property
    def streaming_mode(self) -> StreamingMode:
        """流式输出模式，默认伪流式。"""
        return StreamingMode.PSEUDO

    async def _apply_filter(self, message: OutputMessage) -> OutputMessage:
        """Apply the configured content filter, if any."""
        if self.content_filter is not None:
            return await self.content_filter.apply(message)
        return message

    @property
    @abstractmethod
    def name(self) -> str:
        """适配器名称"""
        pass

    @abstractmethod
    async def send(self, message: OutputMessage, session_id: str) -> None:
        """发送完整输出消息"""
        pass

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """发送流式增量（真流式传输）

        子类应该覆盖此方法以实现真流式（如 WebSocket、SSE、消息编辑等）。
        默认实现是收集到缓冲区，最后一次性发送（伪流式）。

        Args:
            delta: 内容片段
            session_id: 会话ID
            metadata: 可选元数据（如 reasoning 标记等）
        """
        if not hasattr(self, "_delta_buffers"):
            self._delta_buffers: dict[str, list[str]] = {}
        if session_id not in self._delta_buffers:
            self._delta_buffers[session_id] = []
        self._delta_buffers[session_id].append(delta)

    async def flush_deltas(self, session_id: str) -> None:
        """刷新缓冲区，发送收集的内容

        在流式输出结束时调用，将缓冲的内容一次性发送。
        """
        buffers = getattr(self, "_delta_buffers", {})
        if session_id in buffers:
            content = "".join(buffers[session_id])
            if content:
                await self.send(OutputMessage(content=content), session_id)
            del buffers[session_id]


class NullOutputAdapter(OutputAdapter):
    """空输出适配器 - 丢弃所有输出，不发送到任何外部平台。

    适用于 subagent 等内部 Agent，防止其原始 LLM 输出意外泄露到用户界面。
    """

    @property
    def name(self) -> str:
        return "null"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        pass

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        pass

    async def flush_deltas(self, session_id: str) -> None:
        pass

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.NONE


class CLIOutputAdapter(OutputAdapter):
    """CLI 输出适配器 - 真流式输出到终端

    支持实时打印每个 delta，适用于命令行交互场景。
    """

    def __init__(self, prefix: str = "", suffix: str = "\n") -> None:
        self.prefix = prefix
        self.suffix = suffix

    @property
    def name(self) -> str:
        return "cli"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        """发送完整消息到终端"""
        content = message.content or ""
        if content:
            print(f"{self.prefix}{content}", end=self.suffix, flush=True)

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """真流式：立即打印每个 delta 到终端"""
        if delta:
            print(delta, end="", flush=True)

    async def flush_deltas(self, session_id: str) -> None:
        """刷新终端（打印换行）"""
        print(end=self.suffix, flush=True)


class HTTPOutputAdapter(OutputAdapter):
    """HTTP SSE 输出适配器 - 通过 Server-Sent Events 发送流式内容

    每个 delta 会生成一个 SSE 事件，客户端可以通过 EventSource 实时接收。
    """

    def __init__(self, sse_queue: asyncio.Queue | None = None) -> None:
        self.sse_queue = sse_queue or asyncio.Queue()

    @property
    def name(self) -> str:
        return "http_sse"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        """发送完整消息作为 SSE 事件"""
        payload: dict[str, Any] = {
            "type": "message",
            "session_id": session_id,
            "content": message.content,
            "message_type": message.message_type,
        }
        if message.reasoning:
            payload["reasoning"] = message.reasoning
        if message.metadata:
            payload["metadata"] = dict(message.metadata)
        await self.sse_queue.put(f"data: {json.dumps(payload)}\n\n")

    async def send_delta(
        self, delta: str, session_id: str, metadata: dict[str, Any] | None = None
    ) -> None:
        """真流式：每个 delta 作为一个 SSE 事件"""
        payload: dict[str, Any] = {
            "type": "delta",
            "session_id": session_id,
            "content": delta,
        }
        if metadata:
            payload["metadata"] = dict(metadata)
        await self.sse_queue.put(f"data: {json.dumps(payload)}\n\n")

    async def flush_deltas(self, session_id: str) -> None:
        """发送 SSE 完成事件"""
        payload = {
            "type": "flush",
            "session_id": session_id,
        }
        await self.sse_queue.put(f"data: {json.dumps(payload)}\n\n")

    def iter_sse(self):
        """用于 FastAPI StreamingResponse 的异步生成器"""

        async def _generator():
            while True:
                event = await self.sse_queue.get()
                if event is None:
                    break
                yield event

        return _generator()
