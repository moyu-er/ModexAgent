"""`OpenCodeEventParser` — translates OpenCode JSONL events into `Emission`s.

OpenCode emits these event types on stdout (one JSONL line per event):

- ``step_start`` / ``step_finish`` — bookkeeping; no emissions.
- ``text`` — complete text block (not a token delta; OpenCode emits
  only when ``part.time.end`` is set).
- ``reasoning`` — complete reasoning block (same cadence as text).
- ``tool_use`` — tool call + result in one event (OpenCode only emits
  when ``part.state.status`` is ``completed`` or ``error``). The parser
  fans out to ``TOOL_USE`` followed by ``TOOL_RESULT``.
- ``error`` — session error.

The provider-minted session id is captured from the first event that
carries one and exposed via ``captured_session_id`` so the harness can
commit it to the session store after the first turn.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, override
from uuid import uuid4

from ..contracts import ProviderEventParser
from ..events import ExternalCodingEvent
from ..types import Emission

__all__ = ["OpenCodeEventParser"]

_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\\\")


def _strip_ansi(text: str) -> str:
    return _ANSI_PATTERN.sub("", text)


class OpenCodeEventParser(ProviderEventParser):
    """Parse OpenCode's stdout JSONL into `Emission`s."""

    def __init__(self) -> None:
        self._captured_session_id: str | None = None

    @property
    def captured_session_id(self) -> str | None:
        return self._captured_session_id

    @override
    def parse_line(self, line: str) -> Iterator[Emission]:
        try:
            payload: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return iter(())
        if not isinstance(payload, dict):
            return iter(())

        sid = payload.get("sessionID") or payload.get("session_id")
        if isinstance(sid, str) and sid and self._captured_session_id is None:
            self._captured_session_id = sid

        event_type = payload.get("type")
        match event_type:
            case "text":
                return iter(self._handle_text(payload))
            case "reasoning":
                return iter(self._handle_reasoning(payload))
            case "tool_use":
                return iter(self._handle_tool_use(payload))
            case "error":
                return iter(self._handle_error(payload))
            case "step_start" | "step_finish":
                return iter(())
            case _:
                return iter(())

    def _handle_text(self, payload: dict[str, Any]) -> list[Emission]:
        part = payload.get("part")
        text = part.get("text") if isinstance(part, dict) else payload.get("content")
        if not isinstance(text, str) or not text:
            return []
        return [Emission(event=ExternalCodingEvent.TEXT_DELTA, text=text)]

    def _handle_reasoning(self, payload: dict[str, Any]) -> list[Emission]:
        part = payload.get("part")
        text = part.get("text") if isinstance(part, dict) else None
        if not isinstance(text, str) or not text:
            return []
        return [Emission(event=ExternalCodingEvent.THINKING, text=text)]

    def _handle_tool_use(self, payload: dict[str, Any]) -> list[Emission]:
        part = payload.get("part")
        if not isinstance(part, dict):
            return []

        tool_name = part.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return []

        call_id = part.get("callID") or part.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = uuid4().hex[:12]

        state = part.get("state")
        if not isinstance(state, dict):
            return [Emission(
                event=ExternalCodingEvent.TOOL_USE,
                tool_name=tool_name,
                tool_input="{}",
                call_id=call_id,
            )]

        raw_input = state.get("input")
        if isinstance(raw_input, str):
            tool_input = raw_input
        elif isinstance(raw_input, dict):
            tool_input = json.dumps(raw_input, ensure_ascii=False)
        else:
            tool_input = "{}"

        emissions: list[Emission] = [
            Emission(
                event=ExternalCodingEvent.TOOL_USE,
                tool_name=tool_name,
                tool_input=tool_input,
                call_id=call_id,
            )
        ]

        status = state.get("status")
        if status == "error":
            error_msg = state.get("error")
            output = str(error_msg) if error_msg else ""
        elif status == "completed":
            raw_output = state.get("output")
            if isinstance(raw_output, str):
                output = raw_output
            elif isinstance(raw_output, dict | list):
                output = json.dumps(raw_output, ensure_ascii=False)
            else:
                output = str(raw_output) if raw_output is not None else ""
        else:
            output = ""
        if output:
            emissions.append(
                Emission(
                    event=ExternalCodingEvent.TOOL_RESULT,
                    call_id=call_id,
                    output=_strip_ansi(output),
                )
            )
        return emissions

    def _handle_error(self, payload: dict[str, Any]) -> list[Emission]:
        message = payload.get("message")
        if not isinstance(message, str):
            err = payload.get("error")
            if isinstance(err, str):
                message = err
            elif isinstance(err, dict):
                data = err.get("data")
                if isinstance(data, dict) and isinstance(data.get("message"), str):
                    message = data["message"]
                elif isinstance(err.get("message"), str):
                    message = err["message"]
                elif isinstance(err.get("name"), str):
                    message = err["name"]
                else:
                    message = ""
            else:
                message = ""
        return [Emission(event=ExternalCodingEvent.ERROR, message=message)]
