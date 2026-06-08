"""I/O Adapters - 输入输出适配器基类

提供 InputAdapter 和 OutputAdapter 抽象基类，支持多种输入输出源。
"""

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from framework.adapters.platform import StreamingMode

from ..core.types import InputMessage, OutputMessage
from .filters import ContentFilter

logger = logging.getLogger(__name__)


class InputAdapter(ABC):
    """输入适配器基类

    支持多种输入源：QQ、CLI、HTTP、Webhook、消息队列等。

    Subclasses that process raw messages before enqueuing (e.g. QQ, Discord)
    should call ``_try_intercept_control(text, session_id)`` before
    ``_message_queue.put()`` so that /stop and other control commands bypass
    Pipeline entirely.
    """

    def __init__(self) -> None:
        self._control_channel: Any = None
        self._cmd_processor: Any = None
        self._ctrl_output_adapter: Any = None
        self._session_checker: Any = None
        self._turn_uuid_getter: Any = None

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

    def configure_control_filter(
        self,
        *,
        control_channel: Any = None,
        command_processor: Any = None,
        output_adapter: Any = None,
        session_checker: Any = None,
        turn_uuid_getter: Any = None,
    ) -> None:
        """Configure control command interception.

        When configured, ``_try_intercept_control`` routes control slash
        commands (e.g. /stop) to InMemoryControlChannel instead of Pipeline.
        Call this once after the adapter and command processor are created.
        """
        self._control_channel = control_channel
        self._cmd_processor = command_processor
        self._ctrl_output_adapter = output_adapter
        self._session_checker = session_checker
        self._turn_uuid_getter = turn_uuid_getter

    async def _try_intercept_control(self, text: str, session_id: str) -> bool:
        """Try to handle *text* as a control command.  Returns True if handled.

        When a control command (e.g. /stop) is detected it is pushed directly
        into InMemoryControlChannel and acknowledged to the user.  The message
        does NOT enter Pipeline's queue.

        Subclasses call this before ``_message_queue.put()`` in their
        message-receive path.  The default implementation is a no-op when
        ``configure_control_filter`` has not been called.
        """
        processor = self._cmd_processor
        channel = self._control_channel
        output = self._ctrl_output_adapter

        if processor is None or channel is None:
            return False

        parse_result = processor.parse(text)
        if parse_result.invocation is None:
            return False

        # Normalise to canonical session_id (conversation_id:agent_name)
        # so the ControlScope matches what the consumer (agent) uses.
        from framework.multi_agent.session_id import DefaultSessionIdStrategy
        canonical_sid = DefaultSessionIdStrategy().normalize(session_id)

        from framework.commands.constants import BuiltinCommand, CommandDispatchPolicy
        from framework.commands.models import CommandContext

        ctx = CommandContext(
            session_id=canonical_sid,
            input_msg=InputMessage(content=text, session_id=canonical_sid),
            agent_name="main",
        )

        # Workspace switch commands (cd/exit) are handled at the adapter
        # layer — they never trigger agent sessions or change agent state.
        # This avoids self-blocking: the command's own dispatch would
        # otherwise appear as an "active agent" in pool mode.
        if parse_result.invocation.command in (
            BuiltinCommand.CD.value,
            BuiltinCommand.EXIT.value,
        ):
            result = await processor.handle(text, ctx)
            if result.notice and output:
                await output.send(
                    OutputMessage(content=result.notice, session_id=session_id),
                    session_id,
                )
            return True

        policy = processor.dispatch_policy(parse_result.invocation, ctx)
        if policy != CommandDispatchPolicy.BYPASS_QUEUE:
            return False

        # Dedup: already a pending CANCEL_TURN for this session?
        from framework.control.types import ControlCommandType, ControlScope

        existing = await channel.peek(
            ControlScope(session_id=canonical_sid),
            command_types={ControlCommandType.CANCEL_TURN},
        )
        if existing:
            if output:
                await output.send(
                    OutputMessage(
                        content="⏹ Stop already requested.",
                        session_id=session_id,
                    ),
                    session_id,
                )
            return True

        # Activity check: is a turn running?
        checker = self._session_checker
        if checker is not None and not checker(session_id):
            if output:
                await output.send(
                    OutputMessage(
                        content="No running agent turn to stop.",
                        session_id=session_id,
                    ),
                    session_id,
                )
            return True

        # Full handling via command processor
        cmd_result = await processor.handle(text, ctx)
        if cmd_result.control_command is None:
            return False

        # Attach turn_uuid
        uuid_getter = self._turn_uuid_getter
        if uuid_getter is not None:
            turn_uuid = uuid_getter(session_id)
            if turn_uuid is not None:
                cmd_result.control_command.payload["turn_uuid"] = turn_uuid
            else:
                # Turn ended between activity check and UUID fetch.
                if output:
                    await output.send(
                        OutputMessage(
                            content="No running agent turn to stop.",
                            session_id=session_id,
                        ),
                        session_id,
                    )
                return True

        await channel.send(cmd_result.control_command)

        # Ack
        if cmd_result.notice and output:
            await output.send(
                OutputMessage(content=cmd_result.notice, session_id=session_id),
                session_id,
            )

        return True


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

    async def send_delta(self, delta: str, session_id: str, metadata: dict[str, Any] | None = None) -> None:
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



class NullOutputAdapter(OutputAdapter):
    """空输出适配器 - 丢弃所有输出，不发送到任何外部平台。

    适用于 subagent 等内部 Agent，防止其原始 LLM 输出意外泄露到用户界面。
    """

    @property
    def name(self) -> str:
        return "null"

    async def send(self, message: OutputMessage, session_id: str) -> None:
        pass

    async def send_delta(self, delta: str, session_id: str, metadata: dict[str, Any] | None = None) -> None:
        pass

    async def flush_deltas(self, session_id: str) -> None:
        pass

    @property
    def streaming_mode(self) -> StreamingMode:
        return StreamingMode.NONE


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
    def streaming_mode(self) -> StreamingMode:
        return self._inner.streaming_mode

    def _map_session_id(self, session_id: str) -> str:
        if self._separator not in session_id:
            return session_id
        parts = session_id.split(self._separator)
        return parts[0] if self._keep == "first" else self._separator.join(parts[:-1])

    async def send(self, message: OutputMessage, session_id: str) -> None:
        await self._inner.send(message, self._map_session_id(session_id))

    async def send_delta(self, delta: str, session_id: str, metadata: dict[str, Any] | None = None) -> None:
        await self._inner.send_delta(delta, self._map_session_id(session_id), metadata)

    async def flush_deltas(self, session_id: str) -> None:
        await self._inner.flush_deltas(self._map_session_id(session_id))


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

    async def send_delta(self, delta: str, session_id: str, metadata: dict[str, Any] | None = None) -> None:
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

    def __init__(self, sse_queue: asyncio.Queue | None = None):
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

    async def send_delta(self, delta: str, session_id: str, metadata: dict[str, Any] | None = None) -> None:
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
