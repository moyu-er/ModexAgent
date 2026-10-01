"""BufferingSink — delivery-policy sink bridging ``TurnEventSink`` to
``OutputAdapter``.

Text/reasoning events are delivered or buffered according to the
:class:`DeliveryPolicy` derived from the adapter's ``StreamingMode``
(NATIVE / PSEUDO / NONE):

- STREAMING — forward every text delta immediately (true streaming only).
- SEGMENT — buffer, flush on ``IterationFinishedEvent`` (pseudo streaming:
  one message per model segment).
- TURN — buffer, flush on ``TurnFinishedEvent`` (no streaming support).

Terminal events close the turn: ``TurnFinishedEvent`` flushes any residual
buffer, delivers attachments, and clears state; ``TurnErroredEvent`` sends
the error message. Reasoning accumulates into the flush message's metadata,
preserving the historical buffering semantics.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum

from modex_agent.adapters.output import OutputAdapter
from modex_agent.adapters.platform import StreamingMode
from modex_agent.core.emitter import KindGate, TurnEventSink
from modex_agent.core.turn_events import (
    IterationFinishedEvent,
    StopReason,
    TurnErroredEvent,
    TurnEvent,
    TurnFinishedEvent,
    TurnReasoningEvent,
    TurnTextEvent,
)
from modex_agent.messaging.models import OutputMessage

logger = logging.getLogger(__name__)


class DeliveryPolicy(StrEnum):
    """When buffered text is pushed onto the output adapter."""

    STREAMING = "streaming"
    SEGMENT = "segment"
    TURN = "turn"

    @classmethod
    def of_streaming_mode(cls, mode: StreamingMode) -> DeliveryPolicy:
        """Map an adapter ``StreamingMode`` onto the delivery policy.

        Anything that is not explicitly NATIVE (true streaming) or NONE
        (no streaming support) falls back to SEGMENT — the ``OutputAdapter``
        base's PSEUDO default.
        """
        if mode == StreamingMode.NATIVE:
            return cls.STREAMING
        if mode == StreamingMode.NONE:
            return cls.TURN
        return cls.SEGMENT


class BufferingSink(TurnEventSink):
    """Sinks an ``OutputAdapter`` onto the turn-event stream.

    Business channels subclass this and add their own projections (logs,
    transcripts, wire frames) by overriding ``_dispatch`` extensions; the
    base owns content buffering, flush boundaries, attachments delivery,
    and the error path.
    """

    def __init__(
        self,
        output_adapter: OutputAdapter,
        session_id: str,
        gate: KindGate | None = None,
        *,
        policy: DeliveryPolicy | None = None,
        send_timeout: float | None = None,
    ) -> None:
        super().__init__(gate)
        self.output_adapter = output_adapter
        self.session_id = session_id
        # Duck-typed adapters may omit ``streaming_mode`` — the OutputAdapter
        # base's PSEUDO default applies (adapter extension boundary).
        mode = getattr(output_adapter, "streaming_mode", StreamingMode.PSEUDO)
        self._policy = policy or DeliveryPolicy.of_streaming_mode(mode)
        self._send_timeout = send_timeout
        self._content_buffer = ""
        self._reasoning_buffer = ""
        # Per-turn flag: a mid-flight turn_errored already carried the
        # user-facing error message, so the terminal render is suppressed.
        self._error_delivered = False

    def wants_streaming(self) -> bool:
        return self._policy in (DeliveryPolicy.STREAMING, DeliveryPolicy.SEGMENT)

    @property
    def is_true_streaming(self) -> bool:
        """是否是真流式（每个 delta 立即转发，无缓冲）。"""
        return self._policy is DeliveryPolicy.STREAMING

    @property
    def policy(self) -> DeliveryPolicy:
        return self._policy

    async def _safe_adapter_send(self, message: OutputMessage, log_label: str = "send") -> None:
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

    async def _dispatch(self, event: TurnEvent) -> None:
        match event:
            case TurnTextEvent(text=text):
                if not text:
                    return
                if self.is_true_streaming:
                    await self.output_adapter.send_delta(text, self.session_id)
                else:
                    self._content_buffer += text
            case TurnReasoningEvent(text=text):
                if text:
                    self._reasoning_buffer += text
            case IterationFinishedEvent():
                if self._policy is DeliveryPolicy.SEGMENT:
                    await self._flush_buffers()
            case TurnFinishedEvent(stop_reason=stop_reason, error=error, attachments=attachments):
                # Ordering preserved from the retired emitter channels: the
                # error message (when the mid-flight path did not already
                # deliver one), then the residual text flush, then
                # attachments.
                if (
                    stop_reason is StopReason.ERROR
                    and error
                    and not self._error_delivered
                ):
                    self._error_delivered = True
                    await self._safe_adapter_send(
                        OutputMessage(content=f"Error: {error}"),
                        log_label="turn_finished_error",
                    )
                if not self.is_true_streaming:
                    await self._flush_buffers()
                await self._deliver_attachments(attachments)
                self._content_buffer = ""
                self._reasoning_buffer = ""
                self._error_delivered = False
            case TurnErroredEvent(message=message):
                self._error_delivered = True
                await self._safe_adapter_send(
                    OutputMessage(content=f"Error: {message}"),
                    log_label="emit_error",
                )

    async def _deliver_attachments(self, attachments: tuple[str, ...]) -> None:
        """Forward turn attachments (sent explicitly, even in streaming mode)."""
        if attachments:
            await self._safe_adapter_send(
                OutputMessage(content="", attachments=list(attachments)),
                log_label="attachments",
            )

    async def flush(self) -> None:
        if not self.is_true_streaming:
            await self._flush_buffers()

    async def _flush_buffers(self) -> None:
        """刷新缓冲区，发送收集的内容。"""
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


__all__ = [
    "BufferingSink",
    "DeliveryPolicy",
]
