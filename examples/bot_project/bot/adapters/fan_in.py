"""FanInInputAdapter — merges multiple InputAdapter streams into one."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable

from modex_agent.commands.processor import CommandProcessor
from modex_agent.control.channel import InMemoryControlChannel
from modex_agent.core.types import InputMessage
from modex_agent.adapters.output import OutputAdapter
from modex_agent.pipeline.adapters import InputAdapter

logger = logging.getLogger(__name__)


class FanInInputAdapter(InputAdapter):
    """Merges multiple ``InputAdapter`` receive() streams into one.

    Each source adapter's messages are pumped into a shared queue via
    background tasks.  The merged stream is consumed by ``receive()``.

    Control command interception (``_try_intercept_control``) is called
    at the source level before enqueuing.  ``configure_control_filter``
    propagates to all source adapters so interception works regardless
    of which source the server calls it on.

    User-message persistence is handled by the input pipeline
    (``PersistUserMessageStage``), not by this adapter.
    """

    def __init__(self) -> None:
        super().__init__()
        self._merged_queue: asyncio.Queue[InputMessage] = asyncio.Queue()
        self._sources: list[InputAdapter] = []
        self._pump_tasks: list[asyncio.Task[None]] = []

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
        """Start all source adapters and launch pump tasks.

        Per-source isolation: a channel that fails to connect (Telegram
        behind a firewall, QQ auth failure, any future IM) is logged and
        **disabled** here — it never aborts startup of the other sources,
        so the bot stays up on its remaining channels (WebUI, other IM).
        This is the single convergent point that makes any IM connection
        failure non-fatal, regardless of which adapter raised; new IM
        channels inherit it for free.
        """
        started: list[InputAdapter] = []
        for src in self._sources:
            try:
                await src.start()
            except Exception:
                logger.error(
                    "[channel '%s' disabled] failed to start — this channel "
                    "will not send/receive; other channels continue. Cause:",
                    src.name,
                    exc_info=True,
                )
                continue
            started.append(src)
            self._pump_tasks.append(asyncio.create_task(self._pump_source(src)))

        disabled = [s.name for s in self._sources if s not in started]
        if disabled:
            logger.warning(
                "FanIn: %d channel(s) active %s; %d disabled %s",
                len(started),
                [s.name for s in started],
                len(disabled),
                disabled,
            )

    async def stop(self) -> None:
        """Cancel all pump tasks and stop source adapters.

        Each source's stop() is isolated: one failing stop (e.g. an adapter
        that never fully started) must not prevent the others from stopping.
        """
        for task in self._pump_tasks:
            task.cancel()
        self._pump_tasks.clear()
        for src in self._sources:
            try:
                await src.stop()
            except Exception:
                logger.exception("FanIn: stop for channel '%s' failed", src.name)

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
        turn_uuid_getter: Callable[[str], str | None] | None = None,
        turn_canceller: Callable[[str], bool] | None = None,
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
            turn_canceller=turn_canceller,
        )
        for src in self._sources:
            src.configure_control_filter(
                control_channel=control_channel,
                command_processor=command_processor,
                output_adapter=output_adapter,
                session_checker=session_checker,
                turn_uuid_getter=turn_uuid_getter,
                turn_canceller=turn_canceller,
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
        """Background task: forward all messages from *src* to the merged queue."""
        try:
            async for msg in src.receive():
                await self._merged_queue.put(msg)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("FanInInputAdapter: pump for %s crashed", src.name)
