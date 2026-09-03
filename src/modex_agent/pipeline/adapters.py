"""I/O Adapters - 输入适配器基类

提供 InputAdapter 抽象基类，支持多种输入源。
``OutputAdapter`` 家族已迁移至 ``modex_agent.adapters.output``（B4）。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from modex_agent.adapters.output import OutputAdapter
from modex_agent.core.session_id import SessionInfo
from modex_agent.core.types import InputMessage, OutputMessage

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
        self._turn_canceller: Callable[[str], bool] | None = None
        # Set by configure_input_pipeline (default impl); overrides may use
        # different attr names.
        self._input_pipeline: UserInputPipeline | None = None
        self._input_ctx: InputContext | None = None
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
    def receive(self) -> AsyncIterator[InputMessage]:
        """接收输入消息（异步迭代器）"""
        pass

    def configure_input_pipeline(
        self,
        pipeline: UserInputPipeline,
        ctx: InputContext,
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
        turn_canceller: Callable[[str], bool] | None = None,
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
        self._turn_canceller = turn_canceller

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
            input_msg=InputMessage(content=text, session=SessionInfo.from_str(session_id)),
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

        # The control channel is also drained at lifecycle safe points, but a
        # long-running tool may not reach another safe point for minutes. Wake
        # the registered turn task so ToolNode can cancel its active workers
        # immediately and converge through the same cancellation-result path.
        canceller = self._turn_canceller
        if canceller is not None:
            canceller(session_id)

        # Ack
        if cmd_result.notice and output:
            await output.send(
                OutputMessage(content=cmd_result.notice, session_id=session_id),
                session_id,
            )

        return True
