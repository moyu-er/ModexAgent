"""Unit tests for ``OpenCodeV2SseReader`` — persistent V2 SSE reader.

Tests cover SSE event parsing+dispatch, per-session demux (including child
sessions), stall detection, heartbeat stripping, server.connected handling,
durable.seq tracking, dedup, server-down detection, and restart state reset.

All tests use mock SSE streams — no real HTTP server is started.
"""

# ruff: noqa: ANN401

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from modex_agent.agents.external import Emission, ExternalEvent
from modex_agent.agents.external.providers.opencode.v2_parser import (
    OpenCodeV2EventParser,
    OpenCodeV2EventType,
)
from modex_agent.agents.external.providers.opencode.v2_sse_reader import (
    OpenCodeV2SseReader,
)

_BASE_URL = "http://127.0.0.1:4096"
_WORKDIR = "/tmp/test"


# ---------------------------------------------------------------------------
# Mock SSE stream helpers
# ---------------------------------------------------------------------------


class _MockContent:
    """Async line reader mimicking ``aiohttp.StreamReader.readline``."""

    def __init__(self, lines: list[bytes] | None = None, *, block_forever: bool = False) -> None:
        self._lines = list(lines) if lines else []
        self._index = 0
        self._block = block_forever

    async def readline(self) -> bytes:
        if self._block:
            await asyncio.Event().wait()
        if self._index >= len(self._lines):
            return b""
        line = self._lines[self._index]
        self._index += 1
        return line


class _MockResponse:
    """Mimics ``aiohttp.ClientResponse`` for SSE consumption."""

    def __init__(
        self,
        status: int = 200,
        lines: list[bytes] | None = None,
        *,
        block_forever: bool = False,
    ) -> None:
        self.status = status
        self.content = _MockContent(lines, block_forever=block_forever)

    async def __aenter__(self) -> _MockResponse:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


class _ConnectorErrorResponse:
    """Mock response that raises ``ClientConnectorError`` on ``__aenter__``."""

    async def __aenter__(self) -> Any:
        raise aiohttp.ClientConnectorError(
            connection_key=MagicMock(),
            os_error=OSError("Connection refused"),
        )

    async def __aexit__(self, *args: object) -> bool:
        return False


class _MockSession:
    """Mimics ``aiohttp.ClientSession`` — returns queued responses from ``get``."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._responses = list(responses) if responses else []
        self._index = 0
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> Any:
        if self._index >= len(self._responses):
            return _MockResponse(200, block_forever=True)
        resp = self._responses[self._index]
        self._index += 1
        return resp

    async def close(self) -> None:
        self.closed = True


def _data_line(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n"


def _v2_event(
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    event_id: str = "evt_1",
    durable: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": event_id,
        "type": event_type,
        "data": dict(data) if data else {},
    }
    if durable is not None:
        payload["durable"] = durable
    return payload


def _make_reader(
    parser: OpenCodeV2EventParser | None = None,
    *,
    server_url: str = _BASE_URL,
) -> OpenCodeV2SseReader:
    reader = OpenCodeV2SseReader(server_url, _WORKDIR, parser or OpenCodeV2EventParser())
    reader._stall_timeout = 0.1
    reader._reconnect_delay = 0.01
    return reader


def _collect(received: list[Emission]) -> Any:
    async def _cb(emission: Emission) -> None:
        received.append(emission)

    return _cb


# ---------------------------------------------------------------------------
# _process_event: parsing + dispatch
# ---------------------------------------------------------------------------


class TestProcessEventDispatch:
    async def test_text_delta_dispatches_to_registered_callback(self) -> None:
        reader = _make_reader()
        received: list[Emission] = []
        reader.register_session("ses_1", _collect(received))
        reader._stopped = False

        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "Hello"},
                )
            )
        )
        assert len(received) == 1
        assert received[0].event is ExternalEvent.TEXT_DELTA
        assert received[0].text == "Hello"

    async def test_text_delta_async_dispatch(self) -> None:
        reader = _make_reader()
        received: list[Emission] = []
        reader.register_session("ses_1", _collect(received))
        reader._stopped = False

        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "Hi"},
                )
            )
        )
        assert len(received) == 1
        assert received[0].text == "Hi"

    async def test_tool_called_dispatches_tool_use(self) -> None:
        reader = _make_reader()
        received: list[Emission] = []
        reader.register_session("ses_1", _collect(received))
        reader._stopped = False

        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TOOL_CALLED,
                    {"sessionID": "ses_1", "tool": "bash", "callID": "c1", "input": {"cmd": "ls"}},
                )
            )
        )
        assert len(received) == 1
        assert received[0].event is ExternalEvent.TOOL_USE
        assert received[0].tool_name == "bash"

    async def test_server_connected_no_dispatch(self) -> None:
        reader = _make_reader()
        received: list[Emission] = []
        reader.register_session("ses_1", _collect(received))
        reader._stopped = False

        await reader._process_event(json.dumps(_v2_event(OpenCodeV2EventType.SERVER_CONNECTED, {})))
        assert received == []

    async def test_malformed_json_skipped(self) -> None:
        reader = _make_reader()
        reader._stopped = False
        await reader._process_event("not json")
        await reader._process_event('{"type": "x"}')
        await reader._process_event('{"id": "e", "type": "x", "data": "notdict"}')

    async def test_event_for_unregistered_session_dropped(self) -> None:
        reader = _make_reader()
        received: list[Emission] = []
        reader.register_session("ses_1", _collect(received))
        reader._stopped = False

        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_other", "delta": "Hi"},
                )
            )
        )
        assert received == []


class TestPerSessionDemux:
    async def test_main_and_child_session_demux(self) -> None:
        parser = OpenCodeV2EventParser()
        parser.add_main_session("ses_main")
        reader = _make_reader(parser)
        main_recv: list[Emission] = []
        child_recv: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv))
        reader.register_session("ses_child", _collect(child_recv))
        reader._stopped = False

        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_main", "delta": "main text"},
                    event_id="e1",
                )
            )
        )
        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_child", "delta": "child text"},
                    event_id="e2",
                )
            )
        )

        assert len(main_recv) == 1
        assert main_recv[0].text == "main text"
        assert main_recv[0].source_session_id is None
        assert len(child_recv) == 1
        assert child_recv[0].text == "child text"
        assert child_recv[0].source_session_id == "ses_child"

    async def test_two_sessions_routed_correctly(self) -> None:
        reader = _make_reader()
        recv_a: list[Emission] = []
        recv_b: list[Emission] = []
        reader.register_session("ses_a", _collect(recv_a))
        reader.register_session("ses_b", _collect(recv_b))
        reader._stopped = False

        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_a", "delta": "for A"},
                    event_id="e1",
                )
            )
        )
        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_b", "delta": "for B"},
                    event_id="e2",
                )
            )
        )

        assert len(recv_a) == 1 and recv_a[0].text == "for A"
        assert len(recv_b) == 1 and recv_b[0].text == "for B"


# ---------------------------------------------------------------------------
# _consume_stream: heartbeat, stall, SSE line handling
# ---------------------------------------------------------------------------


class TestConsumeStream:
    async def test_heartbeat_lines_stripped(self) -> None:
        reader = _make_reader()
        received: list[Emission] = []
        reader.register_session("ses_1", _collect(received))
        reader._stopped = False

        lines = [
            b": heartbeat\n",
            _data_line(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "Hi"},
                )
            ),
            b"\n",
            b": heartbeat\n",
            b"\n",
        ]
        resp = _MockResponse(200, lines)
        await reader._consume_stream(resp)
        assert len(received) == 1
        assert received[0].text == "Hi"

    async def test_non_data_lines_skipped(self) -> None:
        reader = _make_reader()
        reader._stopped = False

        lines = [
            b"event: message\n",
            b"id: undefined\n",
            b"\n",
        ]
        resp = _MockResponse(200, lines)
        await reader._consume_stream(resp)

    async def test_stall_detection_returns_on_timeout(self) -> None:
        reader = _make_reader()
        reader._stall_timeout = 0.05
        reader._stopped = False

        resp = _MockResponse(200, block_forever=True)
        await reader._consume_stream(resp)

    async def test_eof_returns(self) -> None:
        reader = _make_reader()
        reader._stopped = False

        resp = _MockResponse(200, [])
        await reader._consume_stream(resp)

    async def test_stopped_flag_breaks_loop(self) -> None:
        reader = _make_reader()
        reader._stopped = True
        resp = _MockResponse(200, block_forever=True)
        await reader._consume_stream(resp)


# ---------------------------------------------------------------------------
# durable.seq tracking + dedup
# ---------------------------------------------------------------------------


class TestDurableSeqAndDedup:
    async def test_durable_seq_updates_last_known(self) -> None:
        reader = _make_reader()
        reader.register_session("ses_1", _collect([]))
        reader._stopped = False

        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "Hi"},
                    durable={"aggregateID": "ses_1", "seq": 5, "version": 1},
                )
            )
        )
        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "there"},
                    durable={"aggregateID": "ses_1", "seq": 10, "version": 1},
                    event_id="e2",
                )
            )
        )
        assert reader._last_known_seq["ses_1"] == 10

    async def test_non_durable_event_does_not_update_seq(self) -> None:
        reader = _make_reader()
        reader.register_session("ses_1", _collect([]))
        reader._stopped = False

        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "Hi"},
                    durable={"aggregateID": "ses_1", "seq": 3, "version": 1},
                )
            )
        )
        await reader._process_event(
            json.dumps(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "no durable"},
                    event_id="e2",
                )
            )
        )
        assert reader._last_known_seq["ses_1"] == 3

    async def test_dedup_skips_duplicate_event_id(self) -> None:
        reader = _make_reader()
        received: list[Emission] = []
        reader.register_session("ses_1", _collect(received))
        reader._stopped = False

        event_json = json.dumps(
            _v2_event(
                OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                {"sessionID": "ses_1", "delta": "Hi"},
                event_id="evt_dup",
            )
        )
        await reader._process_event(event_json)
        await reader._process_event(event_json)
        assert len(received) == 1


# ---------------------------------------------------------------------------
# Server-down detection + restart
# ---------------------------------------------------------------------------


class TestServerDownAndRestart:
    async def test_connection_refused_sets_server_unavailable(self) -> None:
        reader = _make_reader()
        reader._http_session = _MockSession([_ConnectorErrorResponse()])
        await reader.start()
        await asyncio.sleep(0.2)
        assert reader._server_unavailable is True
        await reader.stop()

    async def test_restart_resets_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reader = _make_reader(server_url="http://old:4096")
        reader._last_known_seq["ses_1"] = 42
        reader.register_session("ses_1", _collect([]))
        reader._seen_event_ids["ses_1"].add("evt_old")

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.get = MagicMock(return_value=_MockResponse(200, block_forever=True))
        mock_session.close = AsyncMock()
        monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **kw: mock_session)

        await reader.restart("http://new:4096")
        await asyncio.sleep(0.05)

        assert reader._server_url == "http://new:4096"
        assert reader._last_known_seq == {}
        assert reader._seen_event_ids == {"ses_1": set()}
        assert reader._server_unavailable is False
        await reader.stop()


# ---------------------------------------------------------------------------
# register / unregister
# ---------------------------------------------------------------------------


class TestRegisterUnregister:
    def test_register_adds_callback_and_dedup_set(self) -> None:
        reader = _make_reader()
        reader.register_session("ses_1", _collect([]))
        assert "ses_1" in reader._session_callbacks
        assert "ses_1" in reader._seen_event_ids

    def test_unregister_removes_callback_and_dedup_set(self) -> None:
        reader = _make_reader()
        reader.register_session("ses_1", _collect([]))
        reader.unregister_session("ses_1")
        assert "ses_1" not in reader._session_callbacks
        assert "ses_1" not in reader._seen_event_ids


# ---------------------------------------------------------------------------
# Full lifecycle: start → consume → stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_start_consume_stop(self) -> None:
        reader = _make_reader()
        received: list[Emission] = []
        reader.register_session("ses_1", _collect(received))

        lines = [
            _data_line(_v2_event(OpenCodeV2EventType.SERVER_CONNECTED, {})),
            b"\n",
            _data_line(
                _v2_event(
                    OpenCodeV2EventType.SESSION_NEXT_TEXT_DELTA,
                    {"sessionID": "ses_1", "delta": "Hello"},
                    event_id="e2",
                )
            ),
            b"\n",
        ]
        session = _MockSession([_MockResponse(200, lines)])
        reader._http_session = session

        await reader.start()
        await asyncio.sleep(0.1)
        assert len(received) == 1
        assert received[0].text == "Hello"
        await reader.stop()


# ---------------------------------------------------------------------------
# Child session auto-discovery via session.created
# ---------------------------------------------------------------------------


def _v1_event(
    event_type: str,
    properties: dict[str, Any] | None = None,
    *,
    event_id: str = "evt_v1_1",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "type": event_type,
        "properties": dict(properties) if properties else {},
    }


class TestChildSessionDiscovery:
    async def test_session_created_auto_registers_child(self) -> None:
        reader = _make_reader()
        parser = reader._parser
        parser.add_main_session("ses_main")
        main_recv: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv))
        reader._stopped = False

        # session.created for child with parentID matching main session
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "session.created",
                    {
                        "sessionID": "ses_child",
                        "info": {"id": "ses_child", "parentID": "ses_main"},
                    },
                    event_id="evt_create",
                )
            )
        )

        assert "ses_child" in reader._session_callbacks
        assert "ses_child" in reader._seen_event_ids
        assert reader._child_to_parent["ses_child"] == "ses_main"

    async def test_child_event_routed_to_parent_callback(self) -> None:
        reader = _make_reader()
        parser = reader._parser
        parser.add_main_session("ses_main")
        main_recv: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv))
        reader._stopped = False

        # First: session.created discovers the child
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "session.created",
                    {"sessionID": "ses_child", "info": {"id": "ses_child", "parentID": "ses_main"}},
                    event_id="evt_create",
                )
            )
        )

        # Then: child session text delta arrives
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "message.part.delta",
                    {"sessionID": "ses_child", "partID": "p1", "delta": "child text"},
                    event_id="evt_delta",
                )
            )
        )

        assert len(main_recv) == 1
        assert main_recv[0].text == "child text"
        assert main_recv[0].source_session_id == "ses_child"

    async def test_unregistered_child_event_falls_back_to_parent(self) -> None:
        """Even without session.created, child events find their parent
        via the child_to_parent lookup after a manual registration."""
        reader = _make_reader()
        parser = reader._parser
        parser.add_main_session("ses_main")
        main_recv: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv))
        reader._stopped = False

        # Simulate child mapping without session.created event
        reader._child_to_parent["ses_child"] = "ses_main"

        await reader._process_event(
            json.dumps(
                _v1_event(
                    "message.part.delta",
                    {"sessionID": "ses_child", "partID": "p1", "delta": "fallback"},
                    event_id="evt_fb",
                )
            )
        )

        assert len(main_recv) == 1
        assert main_recv[0].text == "fallback"

    def test_unregister_session_cleans_up_children(self) -> None:
        reader = _make_reader()
        reader.register_session("ses_main", _collect([]))
        reader._child_to_parent["ses_child"] = "ses_main"
        reader._session_callbacks["ses_child"] = _collect([])
        reader._seen_event_ids["ses_child"] = set()

        reader.unregister_session("ses_main")

        assert "ses_child" not in reader._session_callbacks
        assert "ses_child" not in reader._seen_event_ids
        assert "ses_child" not in reader._child_to_parent


class TestCrossTurnChildRediscovery:
    """Cross-turn child session lifecycle.

    Turn 1: main registered → child auto-discovered via session.created →
            child events routed → main unregistered (turn end) → child cleaned up.

    Turn 2: main re-registered → child events arrive WITHOUT a new
            session.created (opencode reuses the same child session) →
            emissions silently dropped (child not in session_callbacks,
            not in _child_to_parent).

    This is the expected behavior: opencode's task tool creates NEW child
    sessions each invocation (new session.created event), so the same child
    session_id won't appear in Turn 2 without a new session.created. If
    opencode ever reuses child sessions across prompt_async calls without
    emitting session.created, child emissions would be dropped — a known
    limitation documented here.
    """

    async def test_child_events_dropped_after_turn_end_without_session_created(self) -> None:
        reader = _make_reader()
        parser = reader._parser
        parser.add_main_session("ses_main")
        main_recv: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv))
        reader._stopped = False

        # Turn 1: session.created discovers child
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "session.created",
                    {"sessionID": "ses_child", "info": {"id": "ses_child", "parentID": "ses_main"}},
                    event_id="evt_create_t1",
                )
            )
        )
        # Turn 1: child event routed
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "message.part.delta",
                    {"sessionID": "ses_child", "partID": "p1", "delta": "t1 child"},
                    event_id="evt_delta_t1",
                )
            )
        )
        assert len(main_recv) == 1
        assert main_recv[0].text == "t1 child"

        # Turn 1 end: unregister main → child cleaned up
        reader.unregister_session("ses_main")
        assert "ses_child" not in reader._session_callbacks
        assert "ses_child" not in reader._child_to_parent

        # Turn 2: main re-registered
        main_recv_t2: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv_t2))

        # Turn 2: child event WITHOUT session.created → dropped
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "message.part.delta",
                    {"sessionID": "ses_child", "partID": "p2", "delta": "t2 child"},
                    event_id="evt_delta_t2",
                )
            )
        )
        assert len(main_recv_t2) == 0

    async def test_child_rediscovered_with_new_session_created_in_turn_2(self) -> None:
        reader = _make_reader()
        parser = reader._parser
        parser.add_main_session("ses_main")
        main_recv: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv))
        reader._stopped = False

        # Turn 1: child discovered + event routed
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "session.created",
                    {"sessionID": "ses_child", "info": {"id": "ses_child", "parentID": "ses_main"}},
                    event_id="evt_create_t1",
                )
            )
        )
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "message.part.delta",
                    {"sessionID": "ses_child", "partID": "p1", "delta": "t1"},
                    event_id="evt_delta_t1",
                )
            )
        )
        assert len(main_recv) == 1

        # Turn end: cleanup
        reader.unregister_session("ses_main")

        # Turn 2: main re-registered + NEW session.created for same child_id
        main_recv_t2: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv_t2))

        await reader._process_event(
            json.dumps(
                _v1_event(
                    "session.created",
                    {"sessionID": "ses_child", "info": {"id": "ses_child", "parentID": "ses_main"}},
                    event_id="evt_create_t2",
                )
            )
        )
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "message.part.delta",
                    {"sessionID": "ses_child", "partID": "p2", "delta": "t2"},
                    event_id="evt_delta_t2",
                )
            )
        )
        assert len(main_recv_t2) == 1
        assert main_recv_t2[0].text == "t2"

    async def test_multiple_children_same_turn(self) -> None:
        reader = _make_reader()
        parser = reader._parser
        parser.add_main_session("ses_main")
        main_recv: list[Emission] = []
        reader.register_session("ses_main", _collect(main_recv))
        reader._stopped = False

        # Child 1 discovered
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "session.created",
                    {"sessionID": "ses_c1", "info": {"id": "ses_c1", "parentID": "ses_main"}},
                    event_id="evt_c1_create",
                )
            )
        )
        # Child 1 event
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "message.part.delta",
                    {"sessionID": "ses_c1", "partID": "p1", "delta": "child1 text"},
                    event_id="evt_c1_delta",
                )
            )
        )
        # Child 2 discovered
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "session.created",
                    {"sessionID": "ses_c2", "info": {"id": "ses_c2", "parentID": "ses_main"}},
                    event_id="evt_c2_create",
                )
            )
        )
        # Child 2 event
        await reader._process_event(
            json.dumps(
                _v1_event(
                    "message.part.delta",
                    {"sessionID": "ses_c2", "partID": "p2", "delta": "child2 text"},
                    event_id="evt_c2_delta",
                )
            )
        )

        assert len(main_recv) == 2
        assert main_recv[0].text == "child1 text"
        assert main_recv[0].source_session_id == "ses_c1"
        assert main_recv[1].text == "child2 text"
        assert main_recv[1].source_session_id == "ses_c2"
