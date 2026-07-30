"""``OpenCodeSSEParser`` — translates opencode SSE events into ``Emission``s.

The SSE event stream from ``GET /event`` carries events in the envelope
``{"id": ..., "type": ..., "properties": {...}}``. This parser unwraps the
envelope and translates each event type into zero or more ``Emission``
records.

Event types handled (V1 SSE events from ``opencode serve``):

- ``message.part.delta`` — **incremental** text/reasoning fragments.
  opencode's processor always sets ``field`` to ``"text"`` for both text
  and reasoning parts. The parser tracks ``partID → part.type`` from
  ``message.part.updated`` events to distinguish ``TEXT_DELTA`` from
  ``THINKING``.
- ``message.part.updated`` — part state transitions. For tools, this
  carries the completed/error state. For text/reasoning parts, the
  part's ``type`` is recorded for later delta disambiguation.
- ``session.status`` — turn lifecycle (consumed by the backend, not emitted).
- ``session.error`` — error event → ``ERROR`` emission.
- ``server.connected`` / ``server.heartbeat`` — bookkeeping, no emission.
- ``permission.asked`` — no emission (backend auto-approves via HTTP).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Any, override
from uuid import uuid4

from ..contracts import ProviderEventParser
from ..events import ExternalCodingEvent
from ..types import Emission

__all__ = ["OpenCodeSSEParser", "SSEEventType"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class SSEEventType(StrEnum):
    """Closed set of SSE event type strings from the opencode server."""

    MESSAGE_PART_DELTA = "message.part.delta"
    MESSAGE_PART_UPDATED = "message.part.updated"
    MESSAGE_UPDATED = "message.updated"
    SESSION_STATUS = "session.status"
    SESSION_ERROR = "session.error"
    PERMISSION_ASKED = "permission.asked"
    PERMISSION_REPLIED = "permission.replied"
    QUESTION_ASKED = "question.asked"
    SERVER_CONNECTED = "server.connected"
    SERVER_HEARTBEAT = "server.heartbeat"
    SERVER_INSTANCE_DISPOSED = "server.instance.disposed"


class OpenCodeSSEParser(ProviderEventParser):
    """Parse opencode SSE event JSON into ``Emission``s.

    Events from child sessions (subagent forks) are emitted alongside
    main-session events. The parser tracks child session IDs via
    ``child_session_ids`` so callers can distinguish sources if needed.
    """

    def __init__(self) -> None:
        self._captured_session_id: str | None = None
        self._main_session_id: str | None = None
        self._child_session_ids: set[str] = set()
        self._part_types: dict[str, str] = {}
        self._seen_tool_calls: set[str] = set()

    @property
    def captured_session_id(self) -> str | None:
        return self._captured_session_id

    @property
    def child_session_ids(self) -> frozenset[str]:
        return frozenset(self._child_session_ids)

    def set_main_session(self, session_id: str) -> None:
        self._main_session_id = session_id

    @override
    def parse_line(self, line: str) -> Iterator[Emission]:
        try:
            payload: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return iter(())
        if not isinstance(payload, dict):
            return iter(())

        properties = payload.get("properties")
        if not isinstance(properties, dict):
            return iter(())

        sid = properties.get("sessionID")
        is_child_session = False
        if isinstance(sid, str) and sid:
            if self._captured_session_id is None:
                self._captured_session_id = sid
            if self._main_session_id is not None and sid != self._main_session_id:
                self._child_session_ids.add(sid)
                is_child_session = True

        event_type = payload.get("type")
        match event_type:
            case SSEEventType.MESSAGE_PART_DELTA:
                emissions = self._handle_delta(properties)
            case SSEEventType.MESSAGE_PART_UPDATED:
                emissions = self._handle_part_updated(properties)
            case SSEEventType.SESSION_ERROR:
                emissions = self._handle_error(properties)
            case _:
                emissions = []
        if is_child_session and isinstance(sid, str):
            emissions = [e.model_copy(update={"source_session_id": sid}) for e in emissions]
        return iter(emissions)

    def _handle_delta(self, properties: dict[str, Any]) -> list[Emission]:
        delta = properties.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        part_id = properties.get("partID")
        part_id_str = part_id if isinstance(part_id, str) else None
        part_type = self._part_types.get(part_id_str, "") if part_id_str else ""
        if part_type == "reasoning":
            return [Emission(event=ExternalCodingEvent.THINKING, text=delta, part_id=part_id_str)]
        if part_type == "tool":
            return []
        return [Emission(event=ExternalCodingEvent.TEXT_DELTA, text=delta, part_id=part_id_str)]

    def _handle_part_updated(self, properties: dict[str, Any]) -> list[Emission]:
        part = properties.get("part")
        if not isinstance(part, dict):
            return []
        part_type = part.get("type")
        part_id = part.get("id")
        part_id_str = part_id if isinstance(part_id, str) else None
        if part_id_str and isinstance(part_type, str):
            self._part_types[part_id_str] = part_type
        match part_type:
            case "tool":
                return self._handle_tool(part, part_id_str)
            case _:
                return []

    def _handle_tool(self, part: dict[str, Any], part_id: str | None = None) -> list[Emission]:
        tool_name = part.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return []

        call_id = part.get("callID") or part.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = uuid4().hex[:12]

        state = part.get("state")
        status = state.get("status") if isinstance(state, dict) else None

        emissions: list[Emission] = []

        if call_id not in self._seen_tool_calls:
            if status == "pending":
                return []
            self._seen_tool_calls.add(call_id)
            tool_input = "{}"
            if isinstance(state, dict):
                raw_input = state.get("input")
                if isinstance(raw_input, str):
                    tool_input = raw_input
                elif isinstance(raw_input, dict):
                    tool_input = json.dumps(raw_input, ensure_ascii=False)
            emissions.append(Emission(
                event=ExternalCodingEvent.TOOL_USE,
                tool_name=tool_name,
                tool_input=tool_input,
                call_id=call_id,
                part_id=part_id,
            ))

        if not isinstance(state, dict):
            return emissions

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
                    part_id=part_id,
                )
            )
        return emissions

    def _handle_error(self, properties: dict[str, Any]) -> list[Emission]:
        error = properties.get("error")
        if isinstance(error, str):
            message = error
        elif isinstance(error, dict):
            data = error.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                message = data["message"]
            elif isinstance(error.get("message"), str):
                message = error["message"]
            elif isinstance(error.get("name"), str):
                message = error["name"]
            else:
                message = ""
        else:
            message = ""
        return [Emission(event=ExternalCodingEvent.ERROR, message=message)]
