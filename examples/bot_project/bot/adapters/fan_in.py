"""FanInInputAdapter — merges multiple InputAdapter streams into one."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from typing import TYPE_CHECKING

from framework.commands.processor import CommandProcessor
from framework.control.channel import InMemoryControlChannel
from framework.core.types import InputMessage
from framework.pipeline.adapters import InputAdapter, OutputAdapter

if TYPE_CHECKING:
    from bot.webui.transcript_store import TranscriptStore

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_NAME: str = "main"


class FanInInputAdapter(InputAdapter):
    """Merges multiple ``InputAdapter`` receive() streams into one.

    Each source adapter's messages are pumped into a shared queue via
    background tasks.  The merged stream is consumed by ``receive()``.

    Control command interception (``_try_intercept_control``) is called
    at the source level before enqueuing.  ``configure_control_filter``
    propagates to all source adapters so interception works regardless
    of which source the server calls it on.

    When a ``transcript_store`` is provided, every ``InputMessage`` from
    a non-WebSocket source (QQ, Discord, Slack, etc.) is recorded as a
    ``UserMessageEvent`` so that the WebUI conversation history includes
    user questions — not just assistant responses.  WebSocket messages
    are excluded because ``WebUIServer._ws_send_message`` already writes
    ``UserMessageEvent`` directly.
    """

    def __init__(
        self,
        *,
        transcript_store: TranscriptStore | None = None,
        default_agent_name: str = _DEFAULT_AGENT_NAME,
    ) -> None:
        super().__init__()
        self._merged_queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        self._sources: list[InputAdapter] = []
        self._pump_tasks: list[asyncio.Task[None]] = []
        self._transcript_store: TranscriptStore | None = transcript_store
        self._default_agent_name: str = default_agent_name

    # ------------------------------------------------------------------
    # Source management
    # ------------------------------------------------------------------

    def add_source(self, adapter: InputAdapter) -> None:
        """Register a source input adapter.

        Must be called before ``start()``.
        """
        self._sources.append(adapter)

    @property
    def sources(self) -> list[InputAdapter]:
        return list(self._sources)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return "fan_in"

    async def start(self) -> None:
        """Start all source adapters and launch pump tasks."""
        for src in self._sources:
            await src.start()
            task = asyncio.create_task(self._pump_source(src))
            self._pump_tasks.append(task)

    async def stop(self) -> None:
        """Cancel all pump tasks and stop source adapters."""
        for task in self._pump_tasks:
            task.cancel()
        self._pump_tasks.clear()
        for src in self._sources:
            await src.stop()

    # ------------------------------------------------------------------
    # Control filter propagation
    # ------------------------------------------------------------------

    def configure_control_filter(
        self,
        *,
        control_channel: InMemoryControlChannel | None = None,
        command_processor: CommandProcessor | None = None,
        output_adapter: OutputAdapter | None = None,
        session_checker: Callable[[str], bool] | None = None,
        turn_uuid_getter: Callable[[], str] | None = None,
    ) -> None:
        """Propagate control filter configuration to all source adapters.

        Each source (e.g. WebSocketInputAdapter, QQInputAdapter) may call
        ``_try_intercept_control`` independently, so every source must
        have its filter configured.
        """
        super().configure_control_filter(
            control_channel=control_channel,
            command_processor=command_processor,
            output_adapter=output_adapter,
            session_checker=session_checker,
            turn_uuid_getter=turn_uuid_getter,
        )
        for src in self._sources:
            src.configure_control_filter(
                control_channel=control_channel,
                command_processor=command_processor,
                output_adapter=output_adapter,
                session_checker=session_checker,
                turn_uuid_getter=turn_uuid_getter,
            )

    # ------------------------------------------------------------------
    # Receive
    # ------------------------------------------------------------------

    async def receive(self) -> AsyncIterator[InputMessage]:
        """Yield messages from all sources in arrival order.

        Blocks until the shutdown event is set, then stops iteration.
        """
        while True:
            msg = await self._merged_queue.get()
            yield msg

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _pump_source(self, src: InputAdapter) -> None:
        """Background task: forward all messages from *src* to the merged queue.

        If ``transcript_store`` is configured and the source is NOT the
        WebSocket adapter, each ``InputMessage`` is also persisted as a
        ``UserMessageEvent`` so the WebUI history includes user questions.
        """
        try:
            async for msg in src.receive():
                if self._transcript_store is not None and src.name != "websocket":
                    self._record_user_message(msg)
                await self._merged_queue.put(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("FanInInputAdapter: pump for %s crashed", src.name)

    def _record_user_message(self, msg: InputMessage) -> None:
        """Persist *msg* as a ``UserMessageEvent`` to the transcript store.

        Extracts ``conversation_id`` from ``metadata`` (QQ adapter) or
        falls back to ``session_id`` (generic adapters).
        """
        from bot.webui.events import UserMessageEvent

        content = (msg.content or "").strip()
        if not content:
            return

        conv_id: str = (
            msg.metadata.get("conversation_id", msg.session_id)
            if msg.metadata
            else msg.session_id
        )
        agent_name: str = self._default_agent_name

        event = UserMessageEvent(
            conversation_id=conv_id,
            agent_name=agent_name,
            content=content,
        )
        try:
            self._transcript_store.append(conv_id, agent_name, event)
        except Exception:
            logger.warning(
                "FanInInputAdapter: failed to record UserMessageEvent "
                "for conv=%s agent=%s",
                conv_id,
                agent_name,
                exc_info=True,
            )
