"""EventAssembler — fold an ``LLMStreamEvent`` sequence into one ``LLMResponse``.

Pure accumulator, no I/O: protocol engines translate SSE frames into the
closed six-variant event union, and this module is the single place where an
event sequence becomes a response. Two consumers share it (ADR-0046):
``LLMProvider.chat_stream`` (callback-facade fold) and the React LLM
event loop (inline ``feed`` per event) — the assembled fields must stay
aligned with the legacy ``_stream_with_control`` response shape.

Terminal-event invariant (PRD ch. 6 discipline 3): every stream ends with
exactly one ``Finish`` or one ``StreamFailure``. ``result()`` enforces it —
a feed sequence that runs out without a terminal event yields a synthesized
TIMEOUT error response that keeps the accumulated content. Once a terminal
event has been fed (or ``result()`` has been called), any further ``feed``
raises ``RuntimeError``: fail loud rather than silently merge two streams.

Assembly rules:

- ``content`` / ``reasoning_content`` are the accumulated delta text,
  normalized with ``or None`` on the ``Finish`` path (legacy shape);
  ``completion_start_time`` is the FIRST event's wall-clock time rendered
  as a UTC ISO string.
- ``Finish.replay`` is authoritative for the reasoning fields: signature /
  item_id / encrypted_content come from it when present, and
  ``reasoning_content`` takes the replay value when it is not ``None`` (the
  engine's final value), falling back to the accumulated ``ReasoningDelta``
  text.
- ``StreamFailure.partial_content`` is spliced in FRONT of the accumulated
  content (it is the body prefix that streamed out before the failure); the
  error responses otherwise keep the legacy ``build_timeout_response`` shape
  (content + error + error_info only).
- Tool accumulation is NOT here: engines own the ``ToolStream`` accumulator
  and emit ``ToolCallComplete`` only for finished calls (the LENGTH
  pending-drop rule lives in the engines, tool_stream contract 2).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from modex_agent.core.constants import FinishReason
from modex_agent.core.llm_struct import LLMErrorInfo, LLMErrorKind
from modex_agent.core.stream_events import (
    Finish,
    LLMStreamEvent,
    ReasoningDelta,
    StreamFailure,
    TextDelta,
    ToolCallComplete,
    UsageSnapshot,
)
from modex_agent.core.types import LLMResponse, TokenUsage, ToolCall

__all__ = ["EventAssembler"]

_EOF_WITHOUT_TERMINAL = "stream ended without terminal event"


class EventAssembler:
    """Fold one stream's events into one ``LLMResponse`` (one instance per stream)."""

    def __init__(
        self,
        on_content_delta: Callable[[str], Any] | None = None,
        on_reasoning_delta: Callable[[str], Any] | None = None,
    ) -> None:
        self._on_content_delta = on_content_delta
        self._on_reasoning_delta = on_reasoning_delta
        self._content_parts: list[str] = []
        self._reasoning_parts: list[str] = []
        self._tool_calls: list[ToolCall] = []
        self._usage = TokenUsage()
        self._terminal: Finish | StreamFailure | None = None
        self._first_event_time: float | None = None
        self._closed = False

    async def feed(self, event: LLMStreamEvent) -> None:
        """Dispatch one event onto the accumulator.

        Raises:
            RuntimeError: the stream already reached its terminal state — a
                ``Finish``/``StreamFailure`` was fed, or ``result()`` was
                called. A stream ends with exactly one terminal event.
        """
        if self._closed:
            raise RuntimeError(
                "EventAssembler received an event after the terminal state "
                "(Finish/StreamFailure already fed or result() already called)"
            )
        if self._first_event_time is None:
            self._first_event_time = time.time()
        match event:
            case TextDelta():
                if event.text:
                    self._content_parts.append(event.text)
                    await self._invoke_callback(self._on_content_delta, event.text)
            case ReasoningDelta():
                if event.text:
                    self._reasoning_parts.append(event.text)
                    await self._invoke_callback(self._on_reasoning_delta, event.text)
            case ToolCallComplete():
                self._tool_calls.append(
                    ToolCall(
                        call_id=event.call_id,
                        tool_name=event.tool_name,
                        arguments=event.arguments,
                    )
                )
            case UsageSnapshot():
                # Later snapshots win (engines may emit interim + final usage).
                self._usage = event.usage
            case Finish() | StreamFailure():
                self._terminal = event
                self._closed = True

    def result(self) -> LLMResponse:
        """Assemble the final ``LLMResponse`` from the accumulated state.

        Idempotent — the closed state yields an equal response on every
        call — and closing: a subsequent ``feed`` raises ``RuntimeError``
        even when no terminal event was ever fed (EOF case).
        """
        self._closed = True
        terminal = self._terminal
        match terminal:
            case None:
                # Stream exhausted without Finish/StreamFailure: synthesize
                # the TIMEOUT error response, keeping what arrived.
                return LLMResponse(
                    content="".join(self._content_parts),
                    finish_reason=FinishReason.ERROR,
                    error=_EOF_WITHOUT_TERMINAL,
                    error_info=LLMErrorInfo(
                        kind=LLMErrorKind.TIMEOUT,
                        message=_EOF_WITHOUT_TERMINAL,
                        should_retry=True,
                    ),
                )
            case StreamFailure():
                # partial_content is the pre-failure body prefix — it goes
                # in front of the content accumulated from TextDelta events.
                return LLMResponse(
                    content=terminal.partial_content + "".join(self._content_parts),
                    finish_reason=FinishReason.ERROR,
                    error=terminal.error_info.message,
                    error_info=terminal.error_info,
                )
            case Finish():
                replay = terminal.replay
                reasoning_content = "".join(self._reasoning_parts) or None
                if replay is not None and replay.reasoning_content is not None:
                    # Engine's final value wins over the accumulated deltas.
                    reasoning_content = replay.reasoning_content
                return LLMResponse(
                    content="".join(self._content_parts) or None,
                    tool_calls=self._tool_calls,
                    reasoning_content=reasoning_content,
                    reasoning_signature=(
                        replay.reasoning_signature if replay is not None else None
                    ),
                    reasoning_item_id=replay.reasoning_item_id if replay is not None else None,
                    reasoning_encrypted_content=(
                        replay.reasoning_encrypted_content if replay is not None else None
                    ),
                    finish_reason=terminal.finish_reason,
                    usage=self._usage,
                    completion_start_time=(
                        datetime.fromtimestamp(self._first_event_time, tz=UTC).isoformat()
                        if self._first_event_time is not None
                        else None
                    ),
                )

    @staticmethod
    async def _invoke_callback(callback: Callable[[str], Any] | None, value: str) -> None:
        """Invoke a sync-or-async delta callback (legacy provider pattern)."""
        if callback is None or not value:
            return
        result = callback(value)
        if asyncio.iscoroutine(result):
            await result
