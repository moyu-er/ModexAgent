"""Seam 3 tests — ``POST /api/control/history`` route adapter.

Tests the aiohttp route handler in isolation: a minimal ``web.Application``
with the control route registered, backed by a mock facade. Verifies the
HTTP status codes and ``ControlError`` JSON bodies for each error path:

- Malformed JSON → 400
- ``limit=0`` → 400 (Pydantic validation)
- Missing session → 404
- Agent name mismatch → 409
- Happy path → 200 with serialized ``HistoryResult``
- Empty history → 200 with ``items: []``
- Facade unavailable → 503
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.control.facade import ControlFacadeError
from bot.control.models import (
    ControlError,
    DispatchOutcome,
    HistoryMessage,
    HistoryResult,
    HistorySource,
    SendResult,
)
from bot.control.routes import (
    CONTROL_HISTORY_PATH,
    CONTROL_SEND_PATH,
    register_control_routes,
)

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

_VALID_BODY: dict[str, Any] = {
    "caller": {
        "workspace": "/home/user/project",
        "pool": "coder_pool",
        "session_id": "inv123.coder",
        "agent_name": "coder",
    },
    "limit": 3,
}

_SAMPLE_RESULT = HistoryResult(
    source=HistorySource.MESSAGE_STORE,
    session_id="inv123.coder",
    agent_name="coder",
    pool="coder_pool",
    execution_strategy="react",
    items=[
        HistoryMessage(role="user", content="Hello", message_id="m1", created_at="1000"),
        HistoryMessage(role="assistant", content="Hi", message_id="m2", created_at="2000"),
    ],
    effective_limit=3,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(facade: Any) -> web.Application:  # noqa: ANN401
    """Build a minimal app with the control route registered."""
    app = web.Application()
    register_control_routes(app, facade)
    return app


async def _start_client(app: web.Application) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _facade_returning(result: HistoryResult) -> MagicMock:
    facade = MagicMock()
    facade.history = AsyncMock(return_value=result)
    return facade


def _facade_raising(exc: ControlFacadeError) -> MagicMock:
    facade = MagicMock()
    facade.history = AsyncMock(side_effect=exc)
    return facade


# ---------------------------------------------------------------------------
# Malformed JSON → 400
# ---------------------------------------------------------------------------


class TestMalformedJson:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        app = _make_app(_facade_returning(_SAMPLE_RESULT))
        client = await _start_client(app)
        try:
            resp = await client.post(
                CONTROL_HISTORY_PATH,
                data=b"{not valid json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "malformed_json"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_non_object_json_returns_400(self) -> None:
        app = _make_app(_facade_returning(_SAMPLE_RESULT))
        client = await _start_client(app)
        try:
            resp = await client.post(
                CONTROL_HISTORY_PATH,
                data=b"[1, 2, 3]",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "invalid_request"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# limit=0 → 400 (Pydantic validation)
# ---------------------------------------------------------------------------


class TestLimitValidation:
    @pytest.mark.asyncio
    async def test_limit_zero_returns_400(self) -> None:
        app = _make_app(_facade_returning(_SAMPLE_RESULT))
        client = await _start_client(app)
        try:
            body = {**_VALID_BODY, "limit": 0}
            resp = await client.post(CONTROL_HISTORY_PATH, json=body)
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "validation_error"
            assert "limit" in data["message"]
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_limit_negative_returns_400(self) -> None:
        app = _make_app(_facade_returning(_SAMPLE_RESULT))
        client = await _start_client(app)
        try:
            body = {**_VALID_BODY, "limit": -1}
            resp = await client.post(CONTROL_HISTORY_PATH, json=body)
            assert resp.status == 400
            data = await resp.json()
            assert data["code"] == "validation_error"
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_limit_above_10_returns_400(self) -> None:
        app = _make_app(_facade_returning(_SAMPLE_RESULT))
        client = await _start_client(app)
        try:
            body = {**_VALID_BODY, "limit": 11}
            resp = await client.post(CONTROL_HISTORY_PATH, json=body)
            assert resp.status == 400
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Missing session → 404
# ---------------------------------------------------------------------------


class TestSessionNotFound:
    @pytest.mark.asyncio
    async def test_missing_session_returns_404(self) -> None:
        exc = ControlFacadeError(
            404,
            ControlError(
                code="session_not_found",
                message="Session 'inv123.coder' not found",
            ),
        )
        app = _make_app(_facade_raising(exc))
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_HISTORY_PATH, json=_VALID_BODY)
            assert resp.status == 404
            body = await resp.json()
            assert body["code"] == "session_not_found"
            assert "not found" in body["message"]
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Agent name mismatch → 409
# ---------------------------------------------------------------------------


class TestAgentNameMismatch:
    @pytest.mark.asyncio
    async def test_agent_mismatch_returns_409(self) -> None:
        exc = ControlFacadeError(
            409,
            ControlError(
                code="agent_name_mismatch",
                message="Session bound to 'other', not 'coder'",
            ),
        )
        app = _make_app(_facade_raising(exc))
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_HISTORY_PATH, json=_VALID_BODY)
            assert resp.status == 409
            body = await resp.json()
            assert body["code"] == "agent_name_mismatch"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Happy path → 200
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_returns_200_with_history_result(self) -> None:
        app = _make_app(_facade_returning(_SAMPLE_RESULT))
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_HISTORY_PATH, json=_VALID_BODY)
            assert resp.status == 200
            body = await resp.json()
            assert body["source"] == "message_store"
            assert body["session_id"] == "inv123.coder"
            assert body["agent_name"] == "coder"
            assert body["pool"] == "coder_pool"
            assert body["execution_strategy"] == "react"
            assert body["effective_limit"] == 3
            assert len(body["items"]) == 2
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_items_serialized_with_exclude_none(self) -> None:
        result = HistoryResult(
            source=HistorySource.MESSAGE_STORE,
            session_id="s1.agent",
            agent_name="agent",
            pool="pool",
            execution_strategy="react",
            items=[
                HistoryMessage(role="user", content="hi", message_id="m1"),
            ],
            effective_limit=3,
        )
        app = _make_app(_facade_returning(result))
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_HISTORY_PATH, json=_VALID_BODY)
            assert resp.status == 200
            body = await resp.json()
            item = body["items"][0]
            assert "role" in item
            assert "content" in item
            assert "message_id" in item
            assert "tool_calls" not in item
            assert "tool_call_id" not in item
            assert "tool_name" not in item
            assert "name" not in item
            assert "created_at" not in item
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Empty history → 200 with items: []
# ---------------------------------------------------------------------------


class TestEmptyHistory:
    @pytest.mark.asyncio
    async def test_empty_items_returns_200(self) -> None:
        result = HistoryResult(
            source=HistorySource.MESSAGE_STORE,
            session_id="s1.agent",
            agent_name="agent",
            pool="pool",
            execution_strategy="react",
            items=[],
            effective_limit=3,
        )
        app = _make_app(_facade_returning(result))
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_HISTORY_PATH, json=_VALID_BODY)
            assert resp.status == 200
            body = await resp.json()
            assert body["items"] == []
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Facade unavailable → 503
# ---------------------------------------------------------------------------


class TestFacadeUnavailable:
    @pytest.mark.asyncio
    async def test_no_facade_returns_503(self) -> None:
        app = web.Application()
        app["control_facade"] = None
        from bot.control.routes import handle_history

        app.router.add_post(CONTROL_HISTORY_PATH, handle_history)
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_HISTORY_PATH, json=_VALID_BODY)
            assert resp.status == 503
            body = await resp.json()
            assert body["code"] == "facade_unavailable"
        finally:
            await client.close()


# ---------------------------------------------------------------------------
# Send route (T06 — Seam 3)
# ---------------------------------------------------------------------------


_VALID_SEND_BODY: dict[str, Any] = {
    "caller": {
        "workspace": "/home/user/project",
        "pool": "default",
        "session_id": "conv123.main",
        "agent_name": "main",
    },
    "comm_kind": "normal",
    "target_agent": "coder",
    "content": "hello",
}


def _sample_send_result() -> SendResult:
    return SendResult(
        target_agent="coder",
        target_kind="subagent",
        session_id="inv456.coder",
        invocation_id="inv456",
        dispatch_outcome=DispatchOutcome.NEW_TASK,
        is_peer_send=False,
        is_external_target=False,
        trace_dir=Path("/data/trace"),
    )


class TestSendMalformedJson:
    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self) -> None:
        facade = MagicMock()
        facade.send = AsyncMock(return_value=_sample_send_result())
        app = _make_app(facade)
        client = await _start_client(app)
        try:
            resp = await client.post(
                CONTROL_SEND_PATH,
                data=b"{not valid json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "malformed_json"
        finally:
            await client.close()


class TestSendSelfSend:
    @pytest.mark.asyncio
    async def test_self_send_returns_422(self) -> None:
        exc = ControlFacadeError(
            422,
            ControlError(
                code="self_send_rejected",
                message="Target 'main' is the calling agent itself",
            ),
        )
        facade = MagicMock()
        facade.send = AsyncMock(side_effect=exc)
        app = _make_app(facade)
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_SEND_PATH, json=_VALID_SEND_BODY)
            assert resp.status == 422
            body = await resp.json()
            assert body["code"] == "self_send_rejected"
        finally:
            await client.close()


class TestSendTargetNotFound:
    @pytest.mark.asyncio
    async def test_missing_target_returns_404(self) -> None:
        exc = ControlFacadeError(
            404,
            ControlError(
                code="target_not_found",
                message="Target 'coder' not found in pool communication target store",
            ),
        )
        facade = MagicMock()
        facade.send = AsyncMock(side_effect=exc)
        app = _make_app(facade)
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_SEND_PATH, json=_VALID_SEND_BODY)
            assert resp.status == 404
            body = await resp.json()
            assert body["code"] == "target_not_found"
        finally:
            await client.close()


class TestSendHappyPath:
    @pytest.mark.asyncio
    async def test_returns_200_with_send_result(self) -> None:
        facade = MagicMock()
        facade.send = AsyncMock(return_value=_sample_send_result())
        app = _make_app(facade)
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_SEND_PATH, json=_VALID_SEND_BODY)
            assert resp.status == 200
            body = await resp.json()
            assert body["target_agent"] == "coder"
            assert body["target_kind"] == "subagent"
            assert body["session_id"] == "inv456.coder"
            assert body["invocation_id"] == "inv456"
            assert body["dispatch_outcome"] == "new_task"
            assert body["is_peer_send"] is False
            assert body["is_external_target"] is False
            assert body["trace_dir"] == str(Path("/data/trace"))
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_none_fields_excluded_from_response(self) -> None:
        result = SendResult(
            target_agent="coder",
            target_kind="normal",
            session_id="conv123.coder",
            dispatch_outcome=DispatchOutcome.NOT_APPLICABLE,
            is_peer_send=True,
            is_external_target=False,
        )
        facade = MagicMock()
        facade.send = AsyncMock(return_value=result)
        app = _make_app(facade)
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_SEND_PATH, json=_VALID_SEND_BODY)
            assert resp.status == 200
            body = await resp.json()
            assert "invocation_id" not in body
            assert "trace_dir" not in body
            assert "requested_invocation_id" not in body
        finally:
            await client.close()


class TestSendFacadeUnavailable:
    @pytest.mark.asyncio
    async def test_no_facade_returns_503(self) -> None:
        app = web.Application()
        app["control_facade"] = None
        from bot.control.routes import handle_send

        app.router.add_post(CONTROL_SEND_PATH, handle_send)
        client = await _start_client(app)
        try:
            resp = await client.post(CONTROL_SEND_PATH, json=_VALID_SEND_BODY)
            assert resp.status == 503
            body = await resp.json()
            assert body["code"] == "facade_unavailable"
        finally:
            await client.close()
