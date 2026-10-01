"""``OpenCodeV2EventParser`` — translates opencode SSE events into core ``TurnEvent``s.

The ``/api/event`` SSE stream carries BOTH V2 and V1 events through the same
``EventV2Bridge`` global PubSub. V2 events use the envelope
``{"id", "type", "data": {...}, "durable"?: {...}}``; V1 events use
``{"id", "type", "properties": {...}}``. This parser handles both.

V2 event types (``session.next.*`` — emitted by V2 SessionRunner):
- ``session.next.text.delta`` → ``TurnTextEvent``
- ``session.next.reasoning.delta`` → ``TurnReasoningEvent``
- ``session.next.tool.called`` → ``TurnToolCallEvent``
- ``session.next.tool.success`` → ``TurnToolResultEvent`` (no error)
- ``session.next.tool.failed`` → ``TurnToolResultEvent`` (error filled)

V1 event types (``message.part.*``, ``session.*`` — emitted by V1 SessionPrompt,
which is the execution path for the ``task`` tool / subagent dispatch):
- ``message.part.delta`` → ``TurnTextEvent`` or ``TurnReasoningEvent``
  (part type tracked from prior ``message.part.updated`` events)
- ``message.part.updated`` with ``part.type == "tool"`` →
  ``TurnToolCallEvent`` (first seen) + ``TurnToolResultEvent`` (on
  completed/error)
- ``session.created`` → no event; SSE reader intercepts for child discovery
- ``session.error`` → ``TurnErroredEvent``
- ``server.connected`` — bookkeeping, no event

Provider limits preserved here (moved from the retired emission-mapping
layer in the agent harness):

- **Tool-argument JSON synthesis** — V2 ``tool.called`` carries ``input``
  as arbitrary JSON. When it is not a JSON object (some models stream
  free-text arguments), the arguments are synthesized as
  ``{"input": <raw string>}`` rather than dropped.
- **Tool-name association** — V2 tool success/failed events carry only
  ``callID``, not the tool name; the name is remembered from the
  preceding ``tool.called`` for the same call id and consumed on first
  use. A result whose call id was never seen cannot construct an event
  (tool_name is required) and is dropped with a warning.
- **part_id semantics** — V1 events carry ``partID``; it maps onto the
  ``part_id`` field of text/reasoning/tool events. V2 events carry no
  part identity; their events are emitted with ``part_id=None``.

Child-session routing is NOT a parser concern: core events carry no
session identity, and the SSE reader demuxes per provider session id
through per-session callbacks (see ``v2_sse_reader.py``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter, ValidationError

from modex_agent.core.turn_events import (
    TurnErroredEvent,
    TurnEvent,
    TurnReasoningEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)

__all__ = ["OpenCodeV2EventParser", "OpenCodeV2EventType", "OpenCodeV1EventType"]

logger = logging.getLogger(__name__)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\")

_TOOL_ARGUMENTS_ADAPTER = TypeAdapter(dict[str, JsonValue])


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


class OpenCodeV2EventParser:
    """Parse opencode SSE event JSON (both V2 and V1) into core ``TurnEvent``s.

    The ``/api/event`` stream carries both V2 (``session.next.*``) and V1
    (``message.part.*``, ``session.created``) events. V2 events put the
    payload in ``data``; V1 events put it in ``properties``. This parser
    normalizes both into a single ``dict`` before type-matching.

    For V1 ``message.part.delta``, the part type (text vs reasoning vs tool)
    is not carried in the delta event itself — it must be tracked from prior
    ``message.part.updated`` events via ``partID → part.type``.
    """

    def __init__(self) -> None:
        self._part_types: dict[str, str] = {}
        self._seen_tool_calls: set[str] = set()
        self._call_tool_names: dict[str, str] = {}

    @staticmethod
    def _parse_tool_arguments(raw_input: str) -> dict[str, JsonValue]:
        """Parse a tool-call arguments payload as a JSON object.

        Provider limit: ``input`` may be a non-object JSON value or plain
        text (models that stream free-form arguments). Rather than dropping
        the call, the payload is synthesized as ``{"input": <raw>}`` so the
        observable fact (a call with these arguments) survives.
        """
        raw = raw_input or "{}"
        try:
            return _TOOL_ARGUMENTS_ADAPTER.validate_json(raw)
        except ValidationError:
            return {"input": raw}

    def parse_line(self, line: str) -> Iterator[TurnEvent]:
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

        event_type = payload.get("type")
        match event_type:
            # V2 event types
            case OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA:
                events = self._handle_text_delta(data)
            case OpenCodeV2EventType.SESSION_NEXT_REASONING_DELTA:
                events = self._handle_reasoning_delta(data)
            case OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED:
                events = self._handle_tool_called(data)
            case OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS:
                events = self._handle_tool_success(data)
            case OpenCodeV2EventType.SESSION_NEXT_TOOL_FAILED:
                events = self._handle_tool_failed(data)
            case OpenCodeV2EventType.SESSION_NEXT_TOOL_INPUT_DELTA:
                events = []
            case OpenCodeV2EventType.PERMISSION_V2_ASKED:
                events = []
            case OpenCodeV2EventType.QUESTION_V2_ASKED:
                events = []
            case OpenCodeV2EventType.SESSION_ERROR:
                events = self._handle_session_error(data)
            case OpenCodeV2EventType.SERVER_CONNECTED:
                events = []
            # V1 event types
            case OpenCodeV1EventType.MESSAGE_PART_DELTA:
                events = self._handle_v1_part_delta(data)
            case OpenCodeV1EventType.MESSAGE_PART_UPDATED:
                events = self._handle_v1_part_updated(data)
            case OpenCodeV1EventType.SESSION_CREATED:
                events = []
            case OpenCodeV1EventType.SESSION_ERROR_V1:
                events = self._handle_session_error(data)
            case _:
                events = []
        return iter(events)

    # -- V2 handlers -------------------------------------------------------

    def _handle_text_delta(self, data: dict[str, Any]) -> list[TurnEvent]:
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        return [TurnTextEvent(text=delta)]

    def _handle_reasoning_delta(self, data: dict[str, Any]) -> list[TurnEvent]:
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        return [TurnReasoningEvent(text=delta)]

    def _handle_tool_called(self, data: dict[str, Any]) -> list[TurnEvent]:
        tool_name = data.get("tool")
        if not isinstance(tool_name, str) or not tool_name:
            return []
        call_id = data.get("callID")
        # A call without an id cannot be correlated with its result — the
        # event is not constructible, so it is dropped (parity with the old
        # emission-mapping gate ``if tool_name and call_id``).
        if not isinstance(call_id, str) or not call_id:
            return []
        self._call_tool_names[call_id] = tool_name
        tool_input = self._serialize_tool_input(data.get("input"))
        return [
            TurnToolCallEvent(
                tool_name=tool_name,
                call_id=call_id,
                arguments=self._parse_tool_arguments(tool_input),
            )
        ]

    def _handle_tool_success(self, data: dict[str, Any]) -> list[TurnEvent]:
        output = self._extract_success_output(data)
        if not output:
            return []
        event = self._tool_result_event(data, output, error=None)
        return [event] if event is not None else []

    def _handle_tool_failed(self, data: dict[str, Any]) -> list[TurnEvent]:
        output = self._extract_error_text(data.get("error"))
        if not output:
            return []
        event = self._tool_result_event(data, output, error=output)
        return [event] if event is not None else []

    def _tool_result_event(
        self, data: dict[str, Any], output: str, *, error: str | None
    ) -> TurnToolResultEvent | None:
        call_id = data.get("callID")
        if not isinstance(call_id, str) or not call_id:
            return None
        tool_name = self._call_tool_names.pop(call_id, None)
        if tool_name is None:
            logger.warning(
                "Tool result for call_id=%s has no preceding tool call on record; dropping",
                call_id,
            )
            return None
        return TurnToolResultEvent(
            tool_name=tool_name,
            call_id=call_id,
            output=output,
            error=error,
        )

    def _handle_session_error(self, data: dict[str, Any]) -> list[TurnEvent]:
        message = self._extract_error_text(data.get("error"))
        if not message:
            return []
        return [TurnErroredEvent(message=message)]

    # -- V1 handlers -------------------------------------------------------

    def _handle_v1_part_delta(self, data: dict[str, Any]) -> list[TurnEvent]:
        delta = data.get("delta")
        if not isinstance(delta, str) or not delta:
            return []
        part_id = data.get("partID")
        part_id_str = part_id if isinstance(part_id, str) else None
        part_type = self._part_types.get(part_id_str, "") if part_id_str else ""
        if part_type == "reasoning":
            return [TurnReasoningEvent(text=delta, part_id=part_id_str)]
        if part_type == "tool":
            return []
        return [TurnTextEvent(text=delta, part_id=part_id_str)]

    def _handle_v1_part_updated(self, data: dict[str, Any]) -> list[TurnEvent]:
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

        events: list[TurnEvent] = []

        if call_id not in self._seen_tool_calls:
            if status == "pending":
                return []
            self._seen_tool_calls.add(call_id)
            self._call_tool_names[call_id] = tool_name
            tool_input = "{}"
            if isinstance(state, dict):
                raw_input = state.get("input")
                if isinstance(raw_input, str):
                    tool_input = raw_input
                elif isinstance(raw_input, dict):
                    tool_input = json.dumps(raw_input, ensure_ascii=False)
            events.append(
                TurnToolCallEvent(
                    tool_name=tool_name,
                    call_id=call_id,
                    arguments=self._parse_tool_arguments(tool_input),
                    part_id=part_id_str,
                )
            )

        if not isinstance(state, dict):
            return events

        error: str | None = None
        if status == "error":
            error_msg = state.get("error")
            output = str(error_msg) if error_msg else ""
            error = output or None
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
            self._call_tool_names.pop(call_id, None)
            events.append(
                TurnToolResultEvent(
                    tool_name=tool_name,
                    call_id=call_id,
                    output=_strip_ansi(output),
                    error=error,
                    part_id=part_id_str,
                )
            )
        return events

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
