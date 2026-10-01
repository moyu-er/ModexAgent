"""Parity anchor: native and external planes emit the SAME core event kinds.

The unified turn-event stream only works if a downstream consumer cannot
tell (and never needs to know) which execution plane produced an event.
This test fixes the three semantic facts every plane must express
identically:

- a text delta           → ``TurnTextEvent``
- a tool call            → ``TurnToolCallEvent`` (tool_name, call_id, arguments)
- a tool result          → ``TurnToolResultEvent`` with ``seq`` + ``error``

The native side is anchored by constructing the events exactly as the
native runtime emits them (``react/llm_client.py`` for text,
``react/nodes/tool.py`` for tool calls, ``react/nodes/tool_settlement.py``
for settled results). The external side is produced by the REAL
transport parsing path: the OpenCode SSE parser turns provider wire
records into core events, and the external event normalizer stamps
``seq`` — the same pipeline ``ExternalAgent`` drives.
"""

from __future__ import annotations

import json
from pathlib import Path

from modex_agent.agents.external.normalizer import ExternalEventNormalizer
from modex_agent.agents.external.paths import ExternalPaths
from modex_agent.agents.external.providers.opencode.v2_parser import (
    OpenCodeV2EventParser,
    OpenCodeV2EventType,
)
from modex_agent.agents.external.types import ExternalEnvSpec
from modex_agent.core.emitter import TurnEventSink
from modex_agent.core.turn_events import (
    TurnEvent,
    TurnTextEvent,
    TurnToolCallEvent,
    TurnToolResultEvent,
)


class _RecordingSink(TurnEventSink):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[TurnEvent] = []

    async def _dispatch(self, event: TurnEvent) -> None:
        self.events.append(event)


def _sse(event_type: str, data: dict[str, object]) -> str:
    return json.dumps({"id": "evt", "type": event_type, "data": data})


def _external_events(parser: OpenCodeV2EventParser, lines: list[str]) -> list[TurnEvent]:
    events: list[TurnEvent] = []
    for line in lines:
        events.extend(parser.parse_line(line))
    return events


def _spec(tmp_path: Path) -> ExternalEnvSpec:
    return ExternalEnvSpec(
        workspace_root=tmp_path,
        inbox_root=tmp_path / "inbox",
        workdir=tmp_path,
        session_id="pool1.agent1",
        agent_name="agent1",
        provider_session_id="",
        agent_pool_map={},
        targets=[],
        modexctl_bin_dir=tmp_path / "bin",
    )


class TestTurnEventKindParity:
    async def test_text_maps_to_same_event_kind(self) -> None:
        # Native plane (react/llm_client.py): TurnTextEvent(text=...)
        native = TurnTextEvent(text="hello")

        external = _external_events(
            OpenCodeV2EventParser(),
            [
                _sse(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "hello"},
                )
            ],
        )

        assert external == [native]

    async def test_tool_call_maps_to_same_event_kind(self) -> None:
        # Native plane (react/nodes/tool.py): TurnToolCallEvent(tool_name,
        # call_id, arguments)
        native = TurnToolCallEvent(
            tool_name="read", call_id="call_1", arguments={"path": "/tmp/foo.py"}
        )

        external = _external_events(
            OpenCodeV2EventParser(),
            [
                _sse(
                    OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                    {
                        "sessionID": "ses_1",
                        "callID": "call_1",
                        "tool": "read",
                        "input": {"path": "/tmp/foo.py"},
                    },
                )
            ],
        )

        assert external == [native]

    async def test_tool_result_maps_to_same_event_kind_with_seq_and_error(
        self, tmp_path: Path
    ) -> None:
        # Native plane (react/nodes/tool_settlement.py): TurnToolResultEvent(
        # tool_name, call_id, output, error=..., seq=...)
        native = TurnToolResultEvent(
            tool_name="read",
            call_id="call_1",
            output="File not found",
            error="File not found",
            seq=0,
        )

        # External plane: parser produces the event (error filled from the
        # provider's failed distinction), normalizer stamps the seq.
        parser = OpenCodeV2EventParser()
        parser.parse_line(
            _sse(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                {
                    "sessionID": "ses_1",
                    "callID": "call_1",
                    "tool": "read",
                    "input": {"path": "/tmp/foo.py"},
                },
            )
        )
        parsed = _external_events(
            parser,
            [
                _sse(
                    OpenCodeV2EventType.SESSION_NEXT_TOOL_FAILED,
                    {
                        "sessionID": "ses_1",
                        "callID": "call_1",
                        "error": {"type": "unknown", "message": "File not found"},
                    },
                )
            ],
        )
        sink = _RecordingSink()
        normalizer = ExternalEventNormalizer(
            parent_sink=sink,
            modex_sid="pool1.agent1",
            paths=ExternalPaths(tmp_path),
            spec=_spec(tmp_path),
            base_env={},
        )
        await normalizer.on_event(
            TurnToolCallEvent(tool_name="read", call_id="call_1", arguments={})
        )
        for event in parsed:
            await normalizer.on_event(event)

        delivered = [
            e for e in sink.events if isinstance(e, TurnToolResultEvent)
        ]
        assert delivered == [native]

    async def test_success_tool_result_has_no_error_flag(self, tmp_path: Path) -> None:
        native = TurnToolResultEvent(
            tool_name="read",
            call_id="call_1",
            output="file contents",
            error=None,
            seq=0,
        )

        parser = OpenCodeV2EventParser()
        parser.parse_line(
            _sse(
                OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                {"sessionID": "ses_1", "callID": "call_1", "tool": "read", "input": {}},
            )
        )
        parsed = _external_events(
            parser,
            [
                _sse(
                    OpenCodeV2EventType.SESSION_NEXT_TOOL_SUCCESS,
                    {
                        "sessionID": "ses_1",
                        "callID": "call_1",
                        "content": [{"type": "text", "text": "file contents"}],
                    },
                )
            ],
        )
        sink = _RecordingSink()
        normalizer = ExternalEventNormalizer(
            parent_sink=sink,
            modex_sid="pool1.agent1",
            paths=ExternalPaths(tmp_path),
            spec=_spec(tmp_path),
            base_env={},
        )
        await normalizer.on_event(
            TurnToolCallEvent(tool_name="read", call_id="call_1", arguments={})
        )
        for event in parsed:
            await normalizer.on_event(event)

        delivered = [e for e in sink.events if isinstance(e, TurnToolResultEvent)]
        assert delivered == [native]
