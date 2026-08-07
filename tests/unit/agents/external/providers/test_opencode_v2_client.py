"""Unit tests for :mod:`modex_agent.agents.external.providers.opencode.v2_client`.

Tests mock ``aiohttp.ClientSession.request`` at the wire level — no real HTTP
server is started. Each test constructs a :class:`_MockResp` that mimics the
aiohttp response context manager, then asserts the client parses the body
into the correct typed model or raises :class:`OpencodeV2Error`.
"""

# ruff: noqa: ANN401

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from modex_agent.agents.external.providers.opencode.v2_client import (
    LocationRef,
    ModelRef,
    OpencodeV2Client,
    OpencodeV2Error,
    PermissionRequest,
    PermissionV2Reply,
    PromptInput,
    QuestionRequest,
    SessionActive,
    SessionInputAdmitted,
    SessionsResponse,
    SessionV2Info,
)

# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


class _MockResp:
    """Mimics an aiohttp response context manager."""

    def __init__(self, status: int, body: Any | None) -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> _MockResp:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def text(self) -> str:
        if self._body is None:
            return ""
        return json.dumps(self._body)

    @property
    def closed(self) -> bool:
        return False


def _make_session_mock(
    expected_method: str,
    expected_path_prefix: str,
    responses: list[_MockResp],
) -> MagicMock:
    """Create a MagicMock that behaves like aiohttp.ClientSession.

    Each ``request(method, url, ...)`` call pops the next response from
    ``responses``. The mock asserts the method and URL path match.
    """
    session = MagicMock()
    session.closed = False
    call_iter = iter(responses)

    def _request(method: str, url: str, **kwargs: Any) -> _MockResp:
        assert method == expected_method, f"expected {expected_method}, got {method}"
        assert url.startswith(expected_path_prefix), (
            f"URL {url} does not start with {expected_path_prefix}"
        )
        try:
            return next(call_iter)
        except StopIteration:
            raise AssertionError("No more mock responses queued") from None

    session.request = _request
    session.close = AsyncMock()
    return session


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_BASE_URL = "http://127.0.0.1:4096"

_SESSION_INFO: dict[str, Any] = {
    "id": "ses_abc123",
    "projectID": "prj_test",
    "cost": 0.0,
    "tokens": {
        "input": 100.0,
        "output": 200.0,
        "reasoning": 0.0,
        "cache": {"read": 0.0, "write": 0.0},
    },
    "time": {"created": 1700000000.0, "updated": 1700000000.0},
    "title": "Test Session",
    "location": {"directory": "/tmp/test", "workspaceID": None},
}


def _client(session: MagicMock) -> OpencodeV2Client:
    return OpencodeV2Client(_BASE_URL, session=session)


# ---------------------------------------------------------------------------
# health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_returns_healthy_true_when_body_says_so(self) -> None:
        # Given: health endpoint returns {"healthy": true} (NOT wrapped in data)
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/health",
            [
                _MockResp(200, {"healthy": True}),
            ],
        )
        client = _client(session)

        # When
        result = await client.health()

        # Then
        assert result is True

    async def test_returns_false_when_not_healthy(self) -> None:
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/health",
            [
                _MockResp(200, {"healthy": False}),
            ],
        )
        client = _client(session)

        result = await client.health()
        assert result is False


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    async def test_returns_session_v2_info_from_data(self) -> None:
        # Given
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/api/session",
            [
                _MockResp(200, {"data": _SESSION_INFO}),
            ],
        )
        client = _client(session)

        # When
        result = await client.create_session()

        # Then
        assert isinstance(result, SessionV2Info)
        assert result.id == "ses_abc123"
        assert result.title == "Test Session"
        assert result.tokens.input == 100.0

    async def test_sends_model_and_location_in_body(self) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, url: str, **kwargs: Any) -> _MockResp:
            captured.update(kwargs)
            return _MockResp(200, {"data": _SESSION_INFO})

        session = MagicMock()
        session.closed = False
        session.request = _capture
        session.close = AsyncMock()
        client = _client(session)

        model = ModelRef(id="claude-sonnet", providerID="anthropic")
        location = LocationRef(directory="/tmp/proj")
        await client.create_session(model=model, location=location)

        assert "json" in captured
        assert captured["json"]["model"] == {"id": "claude-sonnet", "providerID": "anthropic"}
        assert captured["json"]["location"]["directory"] == "/tmp/proj"


# ---------------------------------------------------------------------------
# prompt
# ---------------------------------------------------------------------------


class TestPrompt:
    async def test_returns_session_input_admitted(self) -> None:
        # Given
        admitted = {
            "admittedSeq": 1,
            "id": "msg_xyz",
            "sessionID": "ses_abc123",
            "prompt": {"text": "hello"},
            "delivery": "steer",
            "timeCreated": 1700000000.0,
        }
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/api/session/ses_abc123/prompt",
            [
                _MockResp(200, {"data": admitted}),
            ],
        )
        client = _client(session)

        # When
        result = await client.prompt("ses_abc123", PromptInput(text="hello"))

        # Then
        assert isinstance(result, SessionInputAdmitted)
        assert result.id == "msg_xyz"
        assert result.delivery == "steer"
        assert result.prompt.text == "hello"

    async def test_does_not_send_resume_false(self) -> None:
        """resume=False must NEVER be sent — only resume=True is included."""
        captured: dict[str, Any] = {}

        def _capture(method: str, url: str, **kwargs: Any) -> _MockResp:
            captured.update(kwargs)
            return _MockResp(
                200,
                {
                    "data": {
                        "admittedSeq": 0,
                        "id": "msg_1",
                        "sessionID": "ses_1",
                        "prompt": {"text": "hi"},
                        "delivery": "steer",
                        "timeCreated": 0.0,
                    }
                },
            )

        session = MagicMock()
        session.closed = False
        session.request = _capture
        session.close = AsyncMock()
        client = _client(session)

        # resume=None (default) — must not include resume
        await client.prompt("ses_1", PromptInput(text="hi"))
        assert "resume" not in captured["json"]

        # resume=False — must NOT include resume
        await client.prompt("ses_1", PromptInput(text="hi"), resume=False)
        assert "resume" not in captured["json"]

        # resume=True — must include resume=True
        await client.prompt("ses_1", PromptInput(text="hi"), resume=True)
        assert captured["json"]["resume"] is True

    async def test_no_model_parameter_in_payload(self) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, url: str, **kwargs: Any) -> _MockResp:
            captured.update(kwargs)
            return _MockResp(
                200,
                {
                    "data": {
                        "admittedSeq": 0,
                        "id": "msg_1",
                        "sessionID": "ses_1",
                        "prompt": {"text": ""},
                        "delivery": "steer",
                        "timeCreated": 0.0,
                    }
                },
            )

        session = MagicMock()
        session.closed = False
        session.request = _capture
        session.close = AsyncMock()
        client = _client(session)

        await client.prompt("ses_1", PromptInput(text="hi"))
        assert "model" not in captured["json"]


# ---------------------------------------------------------------------------
# get_active_sessions
# ---------------------------------------------------------------------------


class TestGetActiveSessions:
    async def test_returns_dict_of_session_active(self) -> None:
        # Given
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session/active",
            [
                _MockResp(
                    200, {"data": {"ses_a": {"type": "running"}, "ses_b": {"type": "running"}}}
                ),
            ],
        )
        client = _client(session)

        # When
        result = await client.get_active_sessions()

        # Then
        assert len(result) == 2
        assert isinstance(result["ses_a"], SessionActive)
        assert result["ses_a"].type == "running"

    async def test_returns_empty_dict_when_no_active(self) -> None:
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session/active",
            [
                _MockResp(200, {"data": {}}),
            ],
        )
        client = _client(session)

        result = await client.get_active_sessions()
        assert result == {}


# ---------------------------------------------------------------------------
# reply_permission
# ---------------------------------------------------------------------------


class TestReplyPermission:
    async def test_returns_none_on_204(self) -> None:
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/api/session/ses_1/permission/per_1/reply",
            [_MockResp(204, None)],
        )
        client = _client(session)

        result = await client.reply_permission("ses_1", "per_1", PermissionV2Reply.ONCE)
        assert result is None

    async def test_sends_reply_value_in_body(self) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, url: str, **kwargs: Any) -> _MockResp:
            captured.update(kwargs)
            return _MockResp(204, None)

        session = MagicMock()
        session.closed = False
        session.request = _capture
        session.close = AsyncMock()
        client = _client(session)

        await client.reply_permission("ses_1", "per_1", PermissionV2Reply.ALWAYS, message="ok")
        assert captured["json"]["reply"] == "always"
        assert captured["json"]["message"] == "ok"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    async def test_session_not_found_raises_typed_error(self) -> None:
        error_body = {
            "_tag": "SessionNotFoundError",
            "sessionID": "ses_missing",
            "message": "Session not found: ses_missing",
        }
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session/ses_missing",
            [
                _MockResp(404, error_body),
            ],
        )
        client = _client(session)

        with pytest.raises(OpencodeV2Error) as exc_info:
            await client.get_session("ses_missing")

        assert exc_info.value.tag == "SessionNotFoundError"
        assert exc_info.value.status == 404
        assert "not found" in exc_info.value.message.lower()

    async def test_conflict_error_on_prompt(self) -> None:
        error_body = {"_tag": "ConflictError", "message": "conflicting reuse"}
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/api/session/ses_1/prompt",
            [
                _MockResp(409, error_body),
            ],
        )
        client = _client(session)

        with pytest.raises(OpencodeV2Error) as exc_info:
            await client.prompt("ses_1", PromptInput(text="hi"))

        assert exc_info.value.tag == "ConflictError"
        assert exc_info.value.status == 409

    async def test_invalid_request_error_400(self) -> None:
        error_body = {
            "_tag": "InvalidRequestError",
            "message": "bad request",
            "field": "prompt",
        }
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/api/session",
            [
                _MockResp(400, error_body),
            ],
        )
        client = _client(session)

        with pytest.raises(OpencodeV2Error) as exc_info:
            await client.create_session()

        assert exc_info.value.tag == "InvalidRequestError"

    async def test_unauthorized_error_401(self) -> None:
        error_body = {"_tag": "UnauthorizedError", "message": "no auth"}
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session/ses_1",
            [
                _MockResp(401, error_body),
            ],
        )
        client = _client(session)

        with pytest.raises(OpencodeV2Error) as exc_info:
            await client.get_session("ses_1")

        assert exc_info.value.tag == "UnauthorizedError"

    async def test_permission_not_found_on_reply(self) -> None:
        error_body = {
            "_tag": "PermissionNotFoundError",
            "requestID": "per_missing",
            "message": "Permission not found",
        }
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/api/session/ses_1/permission/per_missing/reply",
            [_MockResp(404, error_body)],
        )
        client = _client(session)

        with pytest.raises(OpencodeV2Error) as exc_info:
            await client.reply_permission("ses_1", "per_missing", PermissionV2Reply.REJECT)

        assert exc_info.value.tag == "PermissionNotFoundError"

    async def test_non_json_error_body_falls_back_to_unknown(self) -> None:
        session = MagicMock()
        session.closed = False

        bad_resp = _MockResp(500, None)

        def _req(method: str, url: str, **kwargs: Any) -> _MockResp:
            return bad_resp

        session.request = _req
        session.close = AsyncMock()
        client = _client(session)

        with pytest.raises(OpencodeV2Error) as exc_info:
            await client.get_session("ses_1")

        assert exc_info.value.tag == "UnknownError"
        assert exc_info.value.status == 500


# ---------------------------------------------------------------------------
# list_sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    async def test_returns_sessions_response(self) -> None:
        body = {
            "data": [_SESSION_INFO],
            "cursor": {"previous": None, "next": "cursor_abc"},
        }
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session",
            [
                _MockResp(200, body),
            ],
        )
        # Avoid matching /api/session/active or /api/session/{id} — use a precise check
        client = _client(session)

        result = await client.list_sessions()
        assert isinstance(result, SessionsResponse)
        assert len(result.data) == 1
        assert result.cursor.next == "cursor_abc"

    async def test_passes_query_params(self) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, url: str, **kwargs: Any) -> _MockResp:
            captured.update(kwargs)
            return _MockResp(200, {"data": [], "cursor": {"previous": None, "next": None}})

        session = MagicMock()
        session.closed = False
        session.request = _capture
        session.close = AsyncMock()
        client = _client(session)

        await client.list_sessions(directory="/tmp/proj", limit=10, order="desc")
        assert captured["params"]["directory"] == "/tmp/proj"
        assert captured["params"]["limit"] == "10"
        assert captured["params"]["order"] == "desc"


# ---------------------------------------------------------------------------
# list_pending_permissions / list_pending_questions
# ---------------------------------------------------------------------------


class TestListPendingPermissions:
    async def test_returns_list_of_permission_requests(self) -> None:
        body = {
            "data": [
                {
                    "id": "per_1",
                    "sessionID": "ses_1",
                    "action": "bash",
                    "resources": ["/tmp"],
                }
            ]
        }
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session/ses_1/permission",
            [_MockResp(200, body)],
        )
        client = _client(session)

        result = await client.list_pending_permissions("ses_1")
        assert len(result) == 1
        assert isinstance(result[0], PermissionRequest)
        assert result[0].action == "bash"


class TestListPendingQuestions:
    async def test_returns_list_of_question_requests(self) -> None:
        body = {
            "data": [
                {
                    "id": "que_1",
                    "sessionID": "ses_1",
                    "questions": [
                        {
                            "question": "which?",
                            "header": "Choice",
                            "options": [
                                {"label": "A", "description": "option A"},
                            ],
                        },
                    ],
                }
            ]
        }
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session/ses_1/question",
            [_MockResp(200, body)],
        )
        client = _client(session)

        result = await client.list_pending_questions("ses_1")
        assert len(result) == 1
        assert isinstance(result[0], QuestionRequest)
        assert result[0].questions[0].options[0].label == "A"


# ---------------------------------------------------------------------------
# reject_question / interrupt_session (204 endpoints)
# ---------------------------------------------------------------------------


class TestRejectQuestion:
    async def test_returns_none_on_204(self) -> None:
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/api/session/ses_1/question/que_1/reject",
            [_MockResp(204, None)],
        )
        client = _client(session)

        result = await client.reject_question("ses_1", "que_1")
        assert result is None


class TestInterruptSession:
    async def test_returns_none_on_204(self) -> None:
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/api/session/ses_1/interrupt",
            [_MockResp(204, None)],
        )
        client = _client(session)

        result = await client.interrupt_session("ses_1")
        assert result is None


# ---------------------------------------------------------------------------
# get_context — SessionMessage discriminated union
# ---------------------------------------------------------------------------


class TestGetContext:
    async def test_parses_mixed_message_types(self) -> None:
        body = {
            "data": [
                {
                    "id": "msg_1",
                    "time": {"created": 1700000000.0},
                    "type": "user",
                    "text": "hello",
                },
                {
                    "id": "msg_2",
                    "time": {"created": 1700000001.0},
                    "type": "assistant",
                    "agent": "build",
                    "model": {"id": "claude", "providerID": "anthropic"},
                    "content": [
                        {"type": "text", "id": "part_1", "text": "hi there"},
                    ],
                },
                {
                    "id": "msg_3",
                    "time": {"created": 1700000002.0},
                    "type": "system",
                    "text": "system msg",
                },
            ]
        }
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session/ses_1/context",
            [_MockResp(200, body)],
        )
        client = _client(session)

        result = await client.get_context("ses_1")
        assert len(result) == 3

    async def test_parses_assistant_with_tool_content(self) -> None:
        body = {
            "data": [
                {
                    "id": "msg_1",
                    "time": {"created": 1.0},
                    "type": "assistant",
                    "agent": "build",
                    "model": {"id": "m", "providerID": "p"},
                    "content": [
                        {
                            "type": "tool",
                            "id": "tool_1",
                            "name": "bash",
                            "state": {
                                "status": "completed",
                                "input": {"command": "ls"},
                                "content": [{"type": "text", "text": "file.txt"}],
                                "structured": {},
                            },
                            "time": {"created": 1.0},
                        },
                    ],
                },
            ]
        }
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/api/session/ses_1/context",
            [_MockResp(200, body)],
        )
        client = _client(session)

        result = await client.get_context("ses_1")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------


class TestClose:
    async def test_close_does_not_close_injected_session(self) -> None:
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client = _client(session)

        await client.close()
        session.close.assert_not_awaited()


# ---------------------------------------------------------------------------
# V1 methods — create_session_v1, get_messages_v1, abort_session_v1
# ---------------------------------------------------------------------------


class TestCreateSessionV1:
    async def test_returns_session_id_from_body(self) -> None:
        # Given: V1 POST /session returns {"id": "ses_v1", ...} directly
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/session",
            [
                _MockResp(200, {"id": "ses_v1", "title": "v1 session"}),
            ],
        )
        client = _client(session)

        # When
        result = await client.create_session_v1("/tmp/proj")

        # Then
        assert result == "ses_v1"

    async def test_sends_directory_in_body(self) -> None:
        captured: dict[str, Any] = {}

        def _capture(method: str, url: str, **kwargs: Any) -> _MockResp:
            captured.update(kwargs)
            return _MockResp(200, {"id": "ses_v1"})

        session = MagicMock()
        session.closed = False
        session.request = _capture
        session.close = AsyncMock()
        client = _client(session)

        await client.create_session_v1("/tmp/proj")

        assert captured["json"] == {"directory": "/tmp/proj"}

    async def test_returns_empty_string_when_id_missing(self) -> None:
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/session",
            [
                _MockResp(200, {"title": "no id"}),
            ],
        )
        client = _client(session)

        result = await client.create_session_v1("/tmp/proj")
        assert result == ""


class TestGetMessagesV1:
    async def test_returns_list_when_body_is_list(self) -> None:
        body = [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "hello"}],
            },
        ]
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/session/ses_1/message",
            [_MockResp(200, body)],
        )
        client = _client(session)

        result = await client.get_messages_v1("ses_1")
        assert len(result) == 2
        assert result[0]["info"]["role"] == "user"

    async def test_returns_empty_list_when_body_is_not_list(self) -> None:
        session = _make_session_mock(
            "GET",
            f"{_BASE_URL}/session/ses_1/message",
            [_MockResp(200, {"error": "unexpected dict"})],
        )
        client = _client(session)

        result = await client.get_messages_v1("ses_1")
        assert result == []


class TestAbortSessionV1:
    async def test_returns_none_on_204(self) -> None:
        session = _make_session_mock(
            "POST",
            f"{_BASE_URL}/session/ses_1/abort",
            [_MockResp(204, None)],
        )
        client = _client(session)

        result = await client.abort_session_v1("ses_1")
        assert result is None
