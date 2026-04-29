"""I/O Adapters - 输入输出适配器基类

提供 InputAdapter 和 OutputAdapter 抽象基类，支持多种输入输出源。
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, Optional

from ..core.types import InputMessage, OutputMessage
from .filters import ContentFilter
from framework.adapters.platform import StreamingMode

logger = logging.getLogger(__name__)


class InputAdapter(ABC):
    """输入适配器基类

    支持多种输入源：QQ、CLI、HTTP、Webhook、消息队列等
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """适配器名称"""
        pass

    @abstractmethod
    async def start(self) -> None:
        """启动适配器"""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """停止适配器"""
        pass

    @abstractmethod
    async def receive(self) -> AsyncIterator[InputMessage]:
        """接收输入消息（异步迭代器）"""
        pass


class OutputAdapter(ABC):
    """输出适配器基类

    支持多种输出目标：QQ、CLI、HTTP Response、Webhook、日志等。
    支持真流式输出通过 send_delta() 方法。
    """

    content_filter: Optional[ContentFilter] = None

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

    async def send_delta(self, delta: str, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """发送流式增量（真流式传输）

        子类应该覆盖此方法以实现真流式（如 WebSocket、SSE、消息编辑等）。
        默认实现是收集到缓冲区，最后一次性发送（伪流式）。

        Args:
            delta: 内容片段
            session_id: 会话ID
            metadata: 可选元数据（如 reasoning 标记等）
        """
        if not hasattr(self, '_delta_buffers'):
            self._delta_buffers = {}  # type: ignore
        if session_id not in self._delta_buffers:  # type: ignore
            self._delta_buffers[session_id] = []  # type: ignore
        self._delta_buffers[session_id].append(delta)  # type: ignore

    async def flush_deltas(self, session_id: str) -> None:
        """刷新缓冲区，发送收集的内容

        在流式输出结束时调用，将缓冲的内容一次性发送。
        """
        buffers = getattr(self, '_delta_buffers', {})
        if session_id in buffers:
            content = "".join(buffers[session_id])
            if content:
                await self.send(OutputMessage(content=content), session_id)
            del buffers[session_id]

    @property
    def supports_streaming(self) -> bool:
        """是否支持真流式

        如果子类覆盖了 send_delta() 且有实际实现（非默认缓冲），返回 True。
        """
        # 检查是否覆盖了 send_delta 方法
        return type(self).send_delta is not OutputAdapter.send_delta

    async def send_stream(
        self,
        content_iterator: AsyncIterator[str],
        session_id: str,
    ) -> None:
        """发送流式输出（兼容性方法，默认使用 send_delta）

        子类可以覆盖此方法实现真正的流式发送。
        新方法建议使用 send_delta() 实现更细粒度的控制。
        """
        async for chunk in content_iterator:
            await self.send_delta(chunk, session_id)
        await self.flush_deltas(session_id)


class NullOutputAdapter(OutputAdapter):
    """空输出适配器 - 丢弃所有输出，不发送到任何外部平台。

    适用于 peer agent 等内部 Agent，防止其原始 LLM 输出意外泄露到用户界面。
    """

    @property
    def name(self) -> str:
        return "null"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        pass

    async def send_delta(self, delta: str, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        pass

    async def flush_deltas(self, session_id: str) -> None:
        pass

    @property
    def supports_streaming(self) -> bool:
        return False


class LoggingOutputAdapter(OutputAdapter):
    """日志输出适配器 - 将输出记录到日志，不发送到外部平台。"""

    def __init__(self, level: int = logging.DEBUG):
        self.level = level

    @property
    def name(self) -> str:
        return "logging"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        logger.log(self.level, "[session=%s] %s", session_id, message.content)

    async def send_delta(self, delta: str, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        logger.log(self.level, "[session=%s] delta: %s", session_id, delta)

    async def flush_deltas(self, session_id: str) -> None:
        logger.log(self.level, "[session=%s] flush_deltas", session_id)

    @property
    def supports_streaming(self) -> bool:
        return True


class SessionPrefixStripAdapter(OutputAdapter):
    """剥离 session_id 中内部 agent 名称前缀/后缀的通用适配器。

    AgentPool 内部使用 {conversation_id}:{agent_name} 作为 session_id，
    但外部 I/O 平台（QQ、微信、Discord 等）通常只需要 conversation_id。
    """

    def __init__(self, inner: OutputAdapter, separator: str = ":", keep: str = "first"):
        self._inner = inner
        self._separator = separator
        self._keep = keep  # "first" 或 "last"

    @property
    def name(self) -> str:
        return f"session_prefix_strip:{self._inner.name}"

    @property
    def supports_streaming(self) -> bool:
        return self._inner.supports_streaming

    def _map_session_id(self, session_id: str) -> str:
        if self._separator not in session_id:
            return session_id
        parts = session_id.split(self._separator)
        return parts[0] if self._keep == "first" else self._separator.join(parts[:-1])

    async def send(self, message: OutputMessage, session_id: str) -> None:
        await self._inner.send(message, self._map_session_id(session_id))

    async def send_delta(self, delta: str, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        await self._inner.send_delta(delta, self._map_session_id(session_id), metadata)

    async def flush_deltas(self, session_id: str) -> None:
        await self._inner.flush_deltas(self._map_session_id(session_id))


class CompositeOutputAdapter(OutputAdapter):
    """组合多个输出适配器

    例如：同时输出到 QQ 和日志
    """

    def __init__(self, adapters: list):
        self.adapters = adapters

    @property
    def name(self) -> str:
        return f"composite({', '.join(a.name for a in self.adapters)})"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        for adapter in self.adapters:
            try:
                await adapter.send(message, session_id)
            except Exception as e:
                logger.error(f"Adapter {adapter.name} failed: {e}")

    async def send_delta(self, delta: str, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """转发 send_delta 到所有子适配器"""
        for adapter in self.adapters:
            try:
                await adapter.send_delta(delta, session_id, metadata)
            except Exception as e:
                logger.error(f"Adapter {adapter.name} send_delta failed: {e}")

    async def flush_deltas(self, session_id: str) -> None:
        """转发 flush_deltas 到所有子适配器"""
        for adapter in self.adapters:
            try:
                await adapter.flush_deltas(session_id)
            except Exception as e:
                logger.error(f"Adapter {adapter.name} flush_deltas failed: {e}")

    @property
    def supports_streaming(self) -> bool:
        """如果任一子适配器支持真流式，返回 True"""
        return any(adapter.supports_streaming for adapter in self.adapters)

    async def send_stream(
        self,
        content_iterator: AsyncIterator[str],
        session_id: str,
    ) -> None:
        # 收集流式内容
        chunks = []
        async for chunk in content_iterator:
            chunks.append(chunk)

        content = "".join(chunks)
        await self.send(OutputMessage(content=content), session_id)


class CLIOutputAdapter(OutputAdapter):
    """CLI 输出适配器 - 真流式输出到终端

    支持实时打印每个 delta，适用于命令行交互场景。
    """

    def __init__(self, prefix: str = "", suffix: str = "\n"):
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

    async def send_delta(self, delta: str, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
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

    def __init__(self, sse_queue: Optional[asyncio.Queue] = None):
        self.sse_queue = sse_queue or asyncio.Queue()

    @property
    def name(self) -> str:
        return "http_sse"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        """发送完整消息作为 SSE 事件"""
        payload: Dict[str, Any] = {
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

    async def send_delta(self, delta: str, session_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """真流式：每个 delta 作为一个 SSE 事件"""
        payload: Dict[str, Any] = {
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
