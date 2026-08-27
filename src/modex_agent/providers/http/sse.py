"""SSE frame parsing for direct-HTTP LLM streaming (ADR-0046).

Frame protocols come in two shapes, both carried by :class:`SseFrame`:

- **data-only** (OpenAI chat completions): every event is a bare ``data:``
  line, so ``SseFrame.event`` is ``None``.
- **event + data** (OpenAI Responses, Anthropic): each event pairs an
  ``event:`` line with its ``data:`` line, so ``SseFrame.event`` carries the
  event name.

Caller contract:

- The ``[DONE]`` sentinel (OpenAI chat) arrives as an ordinary frame whose
  data equals :data:`DONE_SENTINEL`; this parser passes it through untouched
  and the protocol engine owns the special-casing.
- EOF without the terminal frame the protocol expects means truncation. The
  parser ends silently — the upper-layer assembler's terminal-event invariant
  decides what the response becomes.
- Comment lines (leading ``:``) and non-``data``/``event`` fields (``id``,
  ``retry``, unknown) never produce frames; they only mean transport
  activity, reported through ``on_activity``.
- An HTTP 200 body whose first line starts with ``{`` is not SSE: the whole
  body is yielded as a single ``event=None`` frame and the caller performs
  error classification on it.
"""

from __future__ import annotations

import codecs
from collections.abc import AsyncIterator, Callable, Iterator

from pydantic import BaseModel, ConfigDict, Field

DONE_SENTINEL = "[DONE]"
"""Data payload OpenAI-chat streams send after the last chunk."""

_BOM = "\ufeff"


class SseFrame(BaseModel):
    """One dispatched SSE event (blank-line terminated)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event: str | None = Field(
        default=None,
        description="SSE event name; None in data-only protocols (OpenAI chat).",
    )
    data: str = Field(
        description="Event payload; a block with multiple data lines is newline-joined.",
    )


async def sse_frames(
    byte_stream: AsyncIterator[bytes],
    on_activity: Callable[[], None] | None = None,
) -> AsyncIterator[SseFrame]:
    """Parse an SSE byte stream into :class:`SseFrame` values.

    Chunk boundaries may fall anywhere — mid UTF-8 sequence, mid line, mid
    CRLF. ``on_activity`` fires once per received chunk and once before each
    yielded frame (transport-liveness signal for watchdog rearming). An
    empty or comment-only stream ends naturally with no frames and no error.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()

    def _activity() -> None:
        if on_activity is not None:
            on_activity()

    # Peek: pull until at least one character decodes (a chunk may carry only
    # part of a multi-byte sequence). A body whose first line starts with "{"
    # is a 200-with-JSON-body, not an SSE frame stream.
    first_text = ""
    async for chunk in byte_stream:
        _activity()
        first_text += decoder.decode(chunk)
        if first_text:
            break
    else:
        return  # Empty body: natural end, no frames, no error.

    if first_text.startswith(_BOM):
        first_text = first_text[1:]

    if first_text.startswith("{"):
        # Whole-body mode: hand the raw body to the caller as one frame for
        # error classification, then stop.
        parts = [first_text]
        async for chunk in byte_stream:
            _activity()
            parts.append(decoder.decode(chunk))
        _activity()
        yield SseFrame(data="".join(parts))
        return

    buffer = first_text
    data_lines: list[str] = []
    event_name: str | None = None

    def _drain_buffer(text: str) -> Iterator[SseFrame]:
        """Consume the complete lines in ``buffer``; yield dispatched frames."""
        nonlocal buffer, event_name
        buffer += text
        *lines, buffer = buffer.split("\n")
        for raw_line in lines:
            line = raw_line.removesuffix("\r")
            if not line:
                # Blank line dispatches the accumulated event; an event name
                # without any data lines is discarded.
                if data_lines:
                    yield SseFrame(event=event_name, data="\n".join(data_lines))
                data_lines.clear()
                event_name = None
            elif not line.startswith(":"):
                field, sep, value = line.partition(":")
                if sep and value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    data_lines.append(value)
                elif field == "event":
                    event_name = value
                # Other fields (id/retry/unknown) are ignored per the SSE spec.

    for frame in _drain_buffer(""):
        _activity()
        yield frame
    async for chunk in byte_stream:
        _activity()
        for frame in _drain_buffer(decoder.decode(chunk)):
            _activity()
            yield frame
