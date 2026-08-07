"""``OpenCodeV2EventParser`` — translates opencode SSE events into ``Emission``s.

The ``/api/event`` SSE stream carries BOTH V2 and V1 events through the same
``EventV2Bridge`` global PubSub. V2 events use the envelope
``{"id", "type", "data": {...}, "durable"?: {...}}``; V1 events use
``{"id", "type", "properties": {...}}``. This parser handles both.

V2 event types (``session.next.*`` — emitted by V2 SessionRunner):
- ``session.next.text.delta`` → ``TEXT_DELTA``
- ``session.next.reasoning.delta`` → ``THINKING``
- ``session.next.tool.called`` → ``TOOL_USE``
- ``session.next.tool.success`` / ``session.next.tool.failed`` → ``TOOL_RESULT``

V1 event types (``message.part.*``, ``session.*`` — emitted by V1 SessionPrompt,
which is the execution path for the ``task`` tool / subagent dispatch):
- ``message.part.delta`` → ``TEXT_DELTA`` or ``THINKING`` (part type tracked
  from prior ``message.part.updated`` events)
- ``message.part.updated`` with ``part.type == "tool"`` → ``TOOL_USE`` (first
  seen) + ``TOOL_RESULT`` (on completed/error)
- ``session.created`` → no emission; SSE reader intercepts for child discovery
- ``session.error`` → ``ERROR`` emission
- ``server.connected`` — bookkeeping, no emission

Child session detection: when the event's ``sessionID`` differs from the main
session, emissions are tagged with ``source_session_id`` for routing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Any, override
from uuid import uuid4

from ...contracts import ProviderEventParser
from ...events import ExternalEvent
from ...types import Emission

__all__ = ["OpenCodeV2EventParser", "OpenCodeV2EventType", "OpenCodeV1EventType"]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class OpenCodeV2EventType(StrEnum):
    """V2 SSE event type strings from the V2 SessionRunner."""

    SESSION_NEXT_TEXT_DELTA = "session.next.text.delta"
    SESSION_NEXT_REASONING_DELTA = "session.next.reasoning.delta"
    SESSION_NEXT_TOOL_CALLED = "session.next.tool.called"
    SESSION_NEXT_TOOL_SUCCESS = "session.next.tool.success"
    SESSION_NEXT_TOOL_FAILED = "session.next.tool.failed"
    SESSION_NEXT_TOOL_INPUT_DELTA = "session.next.tool.input.delta"
    PERMISSION_V2_ASKED = "permission.v2.asked"
    QUESTION_V2_ASKED = "question.v2.asked"
    SESSION_ERROR = "session.error"
    SERVER_CONNECTED = "server.connected"


class OpenCodeV1EventType(StrEnum):
    """V1 SSE event type strings from V1 SessionPrompt (task tool path)."""

    MESSAGE_PART_DELTA = "message.part.delta"
    MESSAGE_PART_UPDATED = "message.part.updated"
    MESSAGE_UPDATED = "message.updated"
    SESSION_CREATED = "session.created"
    SESSION_UPDATED = "session.updated"
    SESSION_STATUS = "session.status"
    SESSION_IDLE = "session.idle"
    SESSION_ERROR_V1 = "session.error"


class OpenCodeV2EventParser(ProviderEventParser):
    """Parse opencode SSE event JSON (both V2 and V1) into ``Emission``s.

    The ``/api/event`` stream carries both V2 (``session.next.*``) and V1
    (``message.part.*``, ``session.created``) events. V2 events put the
    payload in ``data``; V1 events put it in ``properties``. This parser
    normalizes both into a single ``dict`` before type-matching.

    For V1 ``message.part.delta``, the part type (text vs reasoning vs tool)
    is not carried in the delta event itself — it must be tracked from prior
    ``message.part.updated`` events via ``partID → part.type``. This mirrors
    the old V1 parser's ``_part_types`` dict.
    """

    def __init__(self) -> None:
        self._main_session_ids: set[str] = set()
        self._child_session_ids: set[str] = set()
        self._part_types: dict[str, str] = {}
        self._seen_tool_calls: set[str] = set()

    @property
    def child_session_ids(self) -> frozenset[str]:
        return frozenset(self._child_session_ids)

    def add_main_session(self, session_id: str) -> None:
        self._main_session_ids.add(session_id)

    def remove_main_session(self, session_id: str) -> None:
        self._main_session_ids.discard(session_id)

    @override
    def parse_line(self, line: str) -> Iterator[Emission]:
        if line.startswith(":"):
            return iter(())
        try:
            payload: dict[str, Any] = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            return iter(())
        if not isinstance(payload, dict):
            return iter(())

        # Normalize envelope: V2 uses "data", V1 uses "properties".
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload.get("properties")
        if not isinstance(data, dict):
            return iter(())

        sid = data.get("sessionID")
        sid_str = sid if isinstance(sid, str) and sid else None
        is_child_session = (
            sid_str is not None
            and len(self._main_session_ids) > 0
            and sid_str not in self._main_session_ids
        )
        if is_child_session:
            self._child_session_ids.add(sid_str)  # type: ignore[arg-type]

        event_type = payload.get("type")
        match event_type:
            # V2 event types
            case OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA:
                emissions = self._handle_text_delta(data)
            case OpenCodeV2EventType.SESSION_NEXT_REASONING_DELTA:
                emissions = self._handle_reasoning_delta(data)
            case OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED:
                emissions = self._handle_tool_called(data)
            case OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS:
                emissions = self._handle_tool_success(data)
            case OpenCodeV2EventType.SESSION_NEXT_TOOL_FAILED:
                emissions = self._handle_tool_failed(data)
            case OpenCodeV2EventType.SESSION_NEXT_TOOL_INPUT_DELTA:
                emissions = []
            case OpenCodeV2EventType.PERMISSION_V2_ASKED:
                emissions = []
            case OpenCodeV2EventType.QUESTION_V2_ASKED:
                emissions = []
            case OpenCodeV2EventType.SESSION_ERROR:
                emissions = self._handle_session_error(data)
            case OpenCodeV2EventType.SERVER_CONNECTED:
                emissions = []
            # V1 event types
            case OpenCodeV1EventType.MESSAGE_PART_DELTA:
                emissions = self._handle_v1_part_delta(data)
            case OpenCodeV1EventType.MESSAGE_PART_UPDATED:
                emissions = self._handle_v1_part_updated(data)
            case OpenCodeV1EventType.SESSION_CREATED:
                emissions = []
            case OpenCodeV1EventType.SESSION_ERROR_V1:
                emissions = self._handle_session_error(data)
            case _:
                emissions = []
        if is_child_session and sid_str is not None:
            emissions = [e.model_copy(update={"source_session_id": sid_str}) for e in emissions]
        return iter(emissions)

    # -- V2 handlers -------------------------------------------------------

    def _handle_text_delta(self, data: dict[str, Any]) -> list[Emission]:
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        return [Emission(event=ExternalEvent.TEXT_DELTA, text=delta)]

    def _handle_reasoning_delta(self, data: dict[str, Any]) -> list[Emission]:
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        return [Emission(event=ExternalEvent.THINKING, text=delta)]

    def _handle_tool_called(self, data: dict[str, Any]) -> list[Emission]:
        tool_name = data.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return []
        call_id = data.get("callID")
        call_id_str = call_id if isinstance(call_id, str) else None
        tool_input = self._serialize_tool_input(data.get("input"))
        return [
            Emission(
                event=ExternalEvent.TOOL_USE,
                tool_name=tool_name,
                call_id=call_id_str,
                tool_input=tool_input,
            )
        ]

    def _handle_tool_success(self, data: dict[str, Any]) -> list[Emission]:
        output = self._extract_success_output(data)
        if not output:
            return []
        call_id = data.get("callID")
        call_id_str = call_id if isinstance(call_id, str) else None
        return [
            Emission(
                event=ExternalEvent.TOOL_RESULT,
                call_id=call_id_str,
                output=output,
            )
        ]

    def _handle_tool_failed(self, data: dict[str, Any]) -> list[Emission]:
        output = self._extract_error_text(data.get("error"))
        if not output:
            return []
        call_id = data.get("callID")
        call_id_str = call_id if isinstance(call_id, str) else None
        return [
            Emission(
                event=ExternalEvent.TOOL_RESULT,
                call_id=call_id_str,
                output=output,
            )
        ]

    def _handle_session_error(self, data: dict[str, Any]) -> list[Emission]:
        message = self._extract_error_text(data.get("error"))
        return [Emission(event=ExternalEvent.ERROR, message=message)]

    # -- V1 handlers -------------------------------------------------------

    def _handle_v1_part_delta(self, data: dict[str, Any]) -> list[Emission]:
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        part_id = data.get("partID")
        part_id_str = part_id if isinstance(part_id, str) else None
        part_type = self._part_types.get(part_id_str, "") if part_id_str else ""
        if part_type == "reasoning":
            return [Emission(event=ExternalEvent.THINKING, text=delta, part_id=part_id_str)]
        if part_type == "tool":
            return []
        return [Emission(event=ExternalEvent.TEXT_DELTA, text=delta, part_id=part_id_str)]

    def _handle_v1_part_updated(self, data: dict[str, Any]) -> list[Emission]:
        part = data.get("part")
        if not isinstance(part, dict):
            return []
        part_type = part.get("type")
        part_id = part.get("id")
        part_id_str = part_id if isinstance(part_id, str) else None
        if part_id_str and isinstance(part_type, str):
            self._part_types[part_id_str] = part_type

        if part_type != "tool":
            return []

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
            emissions.append(
                Emission(
                    event=ExternalEvent.TOOL_USE,
                    tool_name=tool_name,
                    tool_input=tool_input,
                    call_id=call_id,
                    part_id=part_id,
                )
            )

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
                    event=ExternalEvent.TOOL_RESULT,
                    call_id=call_id,
                    output=_strip_ansi(output),
                    part_id=part_id,
                )
            )
        return emissions

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _serialize_tool_input(raw_input: object) -> str:
        if isinstance(raw_input, str):
            return raw_input
        if isinstance(raw_input, dict):
            return json.dumps(raw_input, ensure_ascii=False)
        return "{}"

    @staticmethod
    def _extract_success_output(data: dict[str, Any]) -> str:
        content = data.get("content")
        if isinstance(content, list) and content:
            texts = [
                item["text"]
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
            if texts:
                return _strip_ansi("".join(texts))
            return json.dumps(content, ensure_ascii=False)
        structured = data.get("structured")
        if isinstance(structured, dict) and structured:
            return json.dumps(structured, ensure_ascii=False)
        return ""

    @staticmethod
    def _extract_error_text(error: object) -> str:
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
            name = error.get("name")
            if isinstance(name, str):
                return name
            return json.dumps(error, ensure_ascii=False)
        return ""
