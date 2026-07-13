"""`PiEventParser` — translates Pi's ``--mode json`` events into `Emission`s.

Pi emits 8 event types on stdout (one JSONL line per event):

- ``agent_start`` / ``turn_start`` / ``turn_end`` / ``auto_retry_end``
  — bookkeeping; no emissions.
- ``message_update`` — partial message with subtype
  ``text_delta`` (→ `ExternalCodingEvent.TEXT_DELTA`) or
  ``thinking_delta`` (→ `ExternalCodingEvent.THINKING`). Rarely a
  single update carries both fields, in which case the parser fans
  out into two emissions.
- ``tool_execution_start`` — `ExternalCodingEvent.TOOL_USE`.
- ``tool_execution_end`` — `ExternalCodingEvent.TOOL_RESULT`.
- ``error`` — `ExternalCodingEvent.ERROR`.

Text deltas may contain Pi-internal markup that must be stripped before
the consumer sees the text:

- ``call:ToolName{args}<tool_call|>`` — opens a tool call (suppress).
- ``|tool_response|>...<|end_tool_response|>`` — closes a tool call (suppress).
- ``<|control_token|>`` — single control marker (drop).

The markup may be split across multiple ``message_update`` lines, so the
parser holds a per-instance buffer and a markup-aware state machine
(`_MarkupStripper` below) that emits clean text only when the buffer
holds a complete plain-text prefix.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from enum import StrEnum
from typing import Any, override

from ..contracts import ProviderEventParser
from ..events import ExternalCodingEvent
from ..types import Emission

__all__ = ["PiEventParser"]


class _MarkupState(StrEnum):
    """Per-instance state for the markup stripper state machine."""

    NORMAL = "normal"
    IN_TOOL_CALL = "in_tool_call"  # saw "call:", waiting for "<tool_call|>"
    IN_TOOL_RESPONSE = "in_tool_response"  # saw "<|tool_response|>", waiting for close


_CONTROL_TOKEN_MARKER = "<|control_token|>"
_TOOL_RESPONSE_OPEN = "<|tool_response|>"
_TOOL_RESPONSE_CLOSE = "<|end_tool_response|>"
_TOOL_CALL_CLOSE = "<tool_call|>"
_TOOL_CALL_OPEN_PREFIX = "call:"


class _MarkupStripper:
    """Stateful text cleaner for Pi's markup tokens.

    The stripper holds a buffer between ``feed`` calls. When a markup
    boundary can be located it emits the clean prefix. When a partial
    marker straddles the buffer's tail it holds the suffix until the
    next call resolves the ambiguity.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._state = _MarkupState.NORMAL

    def feed(self, delta: str) -> str:
        """Append ``delta`` to the buffer and return all clean text now
        safe to emit. Anything left in the buffer is held for the next
        call.
        """
        if not delta:
            return ""
        self._buf += delta
        return self._drain()

    def flush(self) -> str:
        """Return any remaining held text. The stripper does not
        force-emit partial markup — if the buffer still ends in an
        unresolvable marker, the held bytes are returned verbatim and
        the consumer is expected to handle them (Pi's protocol is
        well-formed on day one; a tail flush is only reached on
        stream end after the agent stops emitting).
        """
        remaining = self._buf
        self._buf = ""
        self._state = _MarkupState.NORMAL
        return remaining

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _drain(self) -> str:
        """Process the current buffer; emit and shrink as much as possible."""
        out: list[str] = []
        # Loop until the buffer cannot be advanced.
        while self._buf:
            consumed = self._step(out)
            if consumed == 0:
                break
        # Trim the consumed prefix off the buffer.
        # `_step` reports how many chars it processed; we accumulate in
        # a local cursor to know how much to drop.
        return "".join(out)

    def _step(self, out: list[str]) -> int:
        """One state-machine transition; returns the number of buffer
        chars consumed (0 means "need more input; stop").
        """
        buf = self._buf
        if self._state is _MarkupState.NORMAL:
            # Find the earliest NORMAL-state marker:
            #   - "call:"  → IN_TOOL_CALL
            #   - "<|tool_response|>" → IN_TOOL_RESPONSE
            #   - "<|control_token|>" → stays NORMAL, dropped
            earliest_idx, kind = self._find_nearest_marker(0)
            if earliest_idx is None:
                # No complete marker found — try to hold a partial
                # prefix at the tail.
                partial_start = self._hold_partial_marker(0)
                if partial_start is None:
                    out.append(buf)
                    self._buf = ""
                    return len(buf)
                if partial_start == 0:
                    return 0  # whole buffer is a partial markup prefix; hold it
                # Emit everything before the partial and hold the suffix.
                out.append(buf[:partial_start])
                self._buf = buf[partial_start:]
                return partial_start
            # Emit the prefix before the marker.
            out.append(buf[:earliest_idx])
            marker_len = self._marker_length(kind)
            consumed = earliest_idx + marker_len
            if kind == "call":
                self._state = _MarkupState.IN_TOOL_CALL
            elif kind == "tool_response":
                self._state = _MarkupState.IN_TOOL_RESPONSE
            elif kind == "control_token":
                # Drop and stay NORMAL.
                pass
            self._buf = buf[consumed:]
            return consumed
        if self._state is _MarkupState.IN_TOOL_CALL:
            # Suppress until we see "<tool_call|>".
            idx = buf.find(_TOOL_CALL_CLOSE)
            if idx >= 0:
                consumed = idx + len(_TOOL_CALL_CLOSE)
                self._state = _MarkupState.NORMAL
                self._buf = buf[consumed:]
                return consumed
            # Maybe partial at the tail — hold so we don't lose it.
            partial = self._hold_partial_marker_at_tail(_TOOL_CALL_CLOSE)
            if partial is not None:
                self._buf = buf[partial:]
                return partial
            # Nothing matches at all — drop everything and reset state.
            self._buf = ""
            return len(buf)
        if self._state is _MarkupState.IN_TOOL_RESPONSE:
            # Suppress until we see "<|end_tool_response|>".
            idx = buf.find(_TOOL_RESPONSE_CLOSE)
            if idx >= 0:
                consumed = idx + len(_TOOL_RESPONSE_CLOSE)
                self._state = _MarkupState.NORMAL
                self._buf = buf[consumed:]
                return consumed
            partial = self._hold_partial_marker_at_tail(_TOOL_RESPONSE_CLOSE)
            if partial is not None:
                self._buf = buf[partial:]
                return partial
            self._buf = ""
            return len(buf)
        return 0  # unreachable; defensive

    def _find_nearest_marker(self, pos: int) -> tuple[int | None, str]:
        """Locate the earliest of ``call:``, ``<|tool_response|>``,
        ``<|control_token|>`` at or after ``pos`` in the buffer.

        Returns ``(index, kind)`` where ``kind`` is one of ``"call"``,
        ``"tool_response"``, ``"control_token"``, or ``(None, "")``.
        """
        buf = self._buf
        candidates: list[tuple[int, str]] = []
        idx = buf.find(_TOOL_CALL_OPEN_PREFIX, pos)
        if idx >= 0:
            candidates.append((idx, "call"))
        idx = buf.find(_TOOL_RESPONSE_OPEN, pos)
        if idx >= 0:
            candidates.append((idx, "tool_response"))
        idx = buf.find(_CONTROL_TOKEN_MARKER, pos)
        if idx >= 0:
            candidates.append((idx, "control_token"))
        if not candidates:
            return (None, "")
        candidates.sort(key=lambda pair: pair[0])
        return candidates[0]

    def _marker_length(self, kind: str) -> int:
        if kind == "call":
            return len(_TOOL_CALL_OPEN_PREFIX)
        if kind == "tool_response":
            return len(_TOOL_RESPONSE_OPEN)
        if kind == "control_token":
            return len(_CONTROL_TOKEN_MARKER)
        return 0

    def _hold_partial_marker(self, pos: int) -> int | None:
        """Return the index at which a partial marker prefix starts, or
        ``None`` if no marker prefix is straddling the buffer tail.
        """
        candidates = (
            _TOOL_CALL_OPEN_PREFIX,
            _TOOL_RESPONSE_OPEN,
            _CONTROL_TOKEN_MARKER,
        )
        return self._hold_partial_at_tail_any(pos, candidates)

    def _hold_partial_marker_at_tail(self, marker: str) -> int | None:
        """Return the index where ``marker``'s prefix starts at the buffer
        tail (so the suffix is held), or ``None`` if no prefix matches.
        """
        return self._hold_partial_at_tail_any(0, (marker,))

    def _hold_partial_at_tail_any(
        self, pos: int, markers: tuple[str, ...]
    ) -> int | None:
        buf = self._buf
        best_start: int | None = None
        for marker in markers:
            max_prefix = min(len(buf) - pos, len(marker))
            for plen in range(max_prefix, 0, -1):
                if buf[len(buf) - plen:] == marker[:plen]:
                    start = len(buf) - plen
                    if best_start is None or start < best_start:
                        best_start = start
                    break
        return best_start


class PiEventParser(ProviderEventParser):
    """Parse Pi's stdout JSONL into `Emission`s.

    Captures the provider-minted session id from the first event that
    carries one and exposes it via `captured_session_id` so the harness
    can commit it to the session store after the first turn.

    The parser is stateful: the markup stripper's buffer survives
    across `parse_line` calls so markup tokens split across deltas are
    handled correctly.
    """

    def __init__(self) -> None:
        self._stripper = _MarkupStripper()
        self._captured_session_id: str | None = None

    @property
    def captured_session_id(self) -> str | None:
        """The first provider-minted session id seen, if any."""
        return self._captured_session_id

    @override
    def parse_line(self, line: str) -> Iterator[Emission]:
        try:
            payload: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return iter(())
        if not isinstance(payload, dict):
            return iter(())

        event_type = payload.get("type")
        if event_type == "session":
            sid = payload.get("id")
            if isinstance(sid, str) and sid and self._captured_session_id is None:
                self._captured_session_id = sid
            return iter(())
        sid = payload.get("session_id")
        if isinstance(sid, str) and sid and self._captured_session_id is None:
            self._captured_session_id = sid

        match event_type:
            case "message_update":
                return iter(self._handle_message_update(payload))
            case "tool_execution_start":
                return iter(self._handle_tool_start(payload))
            case "tool_execution_end":
                return iter(self._handle_tool_end(payload))
            case "error":
                return iter(self._handle_error(payload))
            case _:
                return iter(())

    # ------------------------------------------------------------------
    # Per-event handlers
    # ------------------------------------------------------------------

    def _handle_message_update(self, payload: dict[str, Any]) -> list[Emission]:
        emissions: list[Emission] = []

        ame = payload.get("assistantMessageEvent")
        if isinstance(ame, dict):
            return self._handle_assistant_message_event(ame)

        update = payload.get("update")
        if not isinstance(update, dict):
            subtype = payload.get("subtype")
            delta = payload.get("delta")
            if isinstance(delta, str):
                emission = self._emission_for_subtype(subtype, delta)
                if emission is not None:
                    emissions.append(emission)
            return emissions

        text_delta = update.get("text_delta")
        thinking_delta = update.get("thinking_delta")
        if isinstance(text_delta, str):
            cleaned = self._stripper.feed(text_delta)
            if cleaned:
                emissions.append(
                    Emission(event=ExternalCodingEvent.TEXT_DELTA, text=cleaned)
                )
        if isinstance(thinking_delta, str):
            cleaned = self._stripper.feed(thinking_delta)
            if cleaned:
                emissions.append(
                    Emission(event=ExternalCodingEvent.THINKING, text=cleaned)
                )
        if text_delta is not None or thinking_delta is not None:
            return emissions

        subtype = update.get("subtype")
        delta = update.get("delta")
        if isinstance(delta, str):
            emission = self._emission_for_subtype(subtype, delta)
            if emission is not None:
                emissions.append(emission)
        return emissions

    def _handle_assistant_message_event(self, ame: dict[str, Any]) -> list[Emission]:
        sub_type = ame.get("type")
        if sub_type == "text_delta":
            delta = ame.get("delta")
            if isinstance(delta, str) and delta:
                cleaned = self._stripper.feed(delta)
                if cleaned:
                    return [Emission(event=ExternalCodingEvent.TEXT_DELTA, text=cleaned)]
        elif sub_type == "thinking_delta":
            delta = ame.get("delta")
            if isinstance(delta, str) and delta:
                cleaned = self._stripper.feed(delta)
                if cleaned:
                    return [Emission(event=ExternalCodingEvent.THINKING, text=cleaned)]
        return []

    def _emission_for_subtype(
        self, subtype: object, delta: str
    ) -> Emission | None:
        cleaned = self._stripper.feed(delta)
        if not cleaned:
            return None
        if subtype == "thinking_delta":
            return Emission(event=ExternalCodingEvent.THINKING, text=cleaned)
        # Default — text_delta or unknown subtype routes to TEXT_DELTA.
        return Emission(event=ExternalCodingEvent.TEXT_DELTA, text=cleaned)

    def _handle_tool_start(self, payload: dict[str, Any]) -> list[Emission]:
        tool_name = payload.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            return []
        args = payload.get("args", {})
        tool_input = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        # Pi emits tool_call_id on both start and end; carry it on TOOL_USE so
        # the call and its result share one correlation id.
        call_id = payload.get("tool_call_id")
        if not isinstance(call_id, str):
            call_id = None
        return [
            Emission(
                event=ExternalCodingEvent.TOOL_USE,
                tool_name=tool_name,
                tool_input=tool_input,
                call_id=call_id,
            )
        ]

    def _handle_tool_end(self, payload: dict[str, Any]) -> list[Emission]:
        call_id = payload.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id:
            return []
        result = payload.get("result")
        if result is None:
            output = ""
        elif isinstance(result, str):
            output = result
        else:
            output = json.dumps(result, ensure_ascii=False)
        return [
            Emission(
                event=ExternalCodingEvent.TOOL_RESULT,
                call_id=call_id,
                output=output,
            )
        ]

    def _handle_error(self, payload: dict[str, Any]) -> list[Emission]:
        message = payload.get("message")
        if not isinstance(message, str):
            message = payload.get("error")
        if not isinstance(message, str):
            message = ""
        return [Emission(event=ExternalCodingEvent.ERROR, message=message)]
