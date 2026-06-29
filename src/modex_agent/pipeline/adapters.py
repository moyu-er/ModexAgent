"""I/O Adapters - 输入输出适配器基类

提供 InputAdapter 和 OutputAdapter 抽象基类，支持多种输入输出源。
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from modex_agent.adapters.platform import StreamingMode

from modex_agent.core.types import InputMessage, OutputMessage
from modex_agent.core.session_id import SessionInfo
from modex_agent.pipeline.filters import ContentFilter

if TYPE_CHECKING:
    from modex_agent.commands.models import CommandProcessor
    from modex_agent.control.channel import InMemoryControlChannel
    from modex_agent.input_pipeline.context import InputContext
    from modex_agent.input_pipeline.pipeline import UserInputPipeline

logger = logging.getLogger(__name__)


class InputAdapter(ABC):
    """输入适配器基类

    支持多种输入源：QQ、CLI、HTTP、Webhook、消息队列等。

    Control commands (/cd /pool /exit /stop) are intercepted by the
    **input pipeline** stages (``EnvironmentControlStage`` /
    ``SessionControlStage``) via ``ctx.command_adapter._try_intercept_control``
    BEFORE messages reach the queue.  Adapter subclasses should NOT call
    ``_try_intercept_control`` inline — the pipeline stages own that
    responsibility.

    ``configure_control_filter`` must still be called once per adapter to
    inject the control channel and command processor so the stages can
    use ``_try_intercept_control``.
    """

    def __init__(self) -> None:
        self._control_channel: InMemoryControlChannel | None = None
        self._cmd_processor: CommandProcessor | None = None
        self._ctrl_output_adapter: OutputAdapter | None = None
        self._session_checker: Callable[[str], bool] | None = None
        self._turn_uuid_getter: Callable[[str], str | None] | None = None
        # Set by configure_input_pipeline (default impl); overrides may use
        # different attr names.
        self._input_pipeline: "UserInputPipeline | None" = None
        self._input_ctx: "InputContext | None" = None
        self._output_adapter: OutputAdapter | None = None
        # Per-channel current workspace; overridden by subclasses that
        # support workspace switching (e.g. QQ).  Default is CWD.
        self.current_ws: Path = Path.cwd()
        # Home directory for /exit reset; subclasses override.
        self.home: Path = Path.cwd()

    def save_current_ws(self) -> None:
        """Persist current_ws to external storage.

        Default no-op; adapters with per-channel workspace persistence
        (e.g. QQ) override this.
        """
        pass

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

    def configure_input_pipeline(
        self,
        pipeline: "UserInputPipeline",
        ctx: "InputContext",
        output: OutputAdapter | None,
    ) -> None:
        """Inject the converged input pipeline for this channel.

        Default stores *pipeline*, *ctx*, and *output* as instance attributes
        so the adapter's receive loop can run incoming messages through the
        converged stages.  This covers all IM adapters (QQ, Discord, etc.).

        Override with a no-op when the channel is configured elsewhere — e.g.
        WebSocket, whose pipeline is held by the server and dispatched inline
        from ``_ws_send_message``.
        """
        self._input_pipeline = pipeline
        self._input_ctx = ctx
        self._output_adapter = output

    def configure_control_filter(
        self,
        *,
        control_channel: InMemoryControlChannel | None = None,
        command_processor: CommandProcessor | None = None,
        output_adapter: OutputAdapter | None = None,
        session_checker: Callable[[str], bool] | None = None,
        turn_uuid_getter: Callable[[str], str | None] | None = None,
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

        Called by the **input pipeline** stages (``EnvironmentControlStage`` /
        ``SessionControlStage``) via ``ctx.command_adapter._try_intercept_control``,
        NOT by adapter subclasses inline.  Returns False (no-op) when
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

        from modex_agent.commands.constants import CommandDispatchPolicy
        from modex_agent.commands.models import CommandContext

        ctx = CommandContext(
            session_id=session_id,
            input_msg=InputMessage(content=text, session=SessionInfo.from_str(session_id, default_agent_name="main")),
            agent_name="main",
        )

        policy = processor.dispatch_policy(parse_result.invocation, ctx)
        if policy != CommandDispatchPolicy.BYPASS_QUEUE:
            return False

        # Dedup: already a pending CANCEL_TURN for this session?
        from modex_agent.control.types import ControlCommandType, ControlScope

        existing = await channel.peek(
            ControlScope(session_id=session_id),
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
            self._delta_buffers = {}
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
