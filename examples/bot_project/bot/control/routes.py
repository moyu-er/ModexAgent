"""aiohttp route adapters for the control API (T04).

Thin adapters that parse JSON → Pydantic → call :class:`BotControlFacade` →
serialize. All error paths return a :class:`ControlError` JSON body with the
appropriate HTTP status (400/404/409/422/500).

Registered on the ``WebUIServer`` application alongside the existing REST
routes via :meth:`WebUIServer.set_control_facade`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web
from pydantic import ValidationError

from bot.control.facade import BotControlFacade, ControlFacadeError
from bot.control.models import (
    ControlError,
    HistoryRequest,
    HistoryResult,
    SendRequest,
    SendResult,
)


logger = logging.getLogger(__name__)

#: Path registered on the aiohttp router.
CONTROL_HISTORY_PATH: str = "/api/control/history"

#: Path for the send route (T06).
CONTROL_SEND_PATH: str = "/api/control/send"


def _json_error(status: int, error: ControlError) -> web.Response:
    """Build a JSON :class:`web.Response` from a :class:`ControlError`."""
    return web.json_response(error.model_dump(), status=status)


def _control_error_from_exception(exc: Exception) -> tuple[int, ControlError]:
    """Classify an unexpected exception into a 500 ``ControlError``."""
    logger.exception("Unhandled error in control route")
    return 500, ControlError(code="internal_error", message=str(exc))


async def handle_history(request: web.Request) -> web.Response:
    """``POST /api/control/history`` — thin adapter to the facade.

    Steps:
        1. Read + parse the JSON body (400 on malformed JSON).
        2. Validate via :class:`HistoryRequest` (400 on Pydantic error,
           including ``limit=0``).
        3. Call :meth:`BotControlFacade.history` (404/409/422 via
           :class:`ControlFacadeError`).
        4. Serialize :class:`HistoryResult` with ``exclude_none=True`` on
           the items (Server Projection).
    """
    facade: BotControlFacade | None = request.app["control_facade"]
    if facade is None:
        return _json_error(
            503,
            ControlError(
                code="facade_unavailable",
                message="Control facade is not wired on this server",
            ),
        )

    # 1. Read + parse JSON body.
    try:
        raw_body = await request.read()
        payload: dict[str, Any] = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _json_error(
            400,
            ControlError(
                code="malformed_json",
                message=f"Request body is not valid JSON: {exc}",
            ),
        )
    if not isinstance(payload, dict):
        return _json_error(
            400,
            ControlError(
                code="invalid_request",
                message="Request body must be a JSON object",
            ),
        )

    # 2. Validate via Pydantic.
    try:
        history_request = HistoryRequest.model_validate(payload)
    except ValidationError as exc:
        # Flatten the Pydantic error list into a single message so the
        # CLI sees a plain string (not a JSON array of errors).
        errors = exc.errors()
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors
        )
        return _json_error(
            400,
            ControlError(
                code="validation_error",
                message=detail or "Request validation failed",
            ),
        )

    # 3. Call the facade.
    try:
        result: HistoryResult = await facade.history(history_request)
    except ControlFacadeError as exc:
        return _json_error(exc.status, exc.error)
    except Exception as exc:
        status, error = _control_error_from_exception(exc)
        return _json_error(status, error)

    # 4. Serialize with exclude_none on items (Server Projection).
    body = result.model_dump()
    body["items"] = [m.model_dump(exclude_none=True) for m in result.items]
    return web.json_response(body)


async def handle_send(request: web.Request) -> web.Response:
    """``POST /api/control/send`` — thin adapter to the facade.

    Same parse → validate → call → serialize pattern as ``handle_history``.
    Error paths: 400 (malformed JSON / validation), 404 (target/pool not
    found), 422 (self-send rejected), 503 (facade unavailable).
    """
    facade: BotControlFacade | None = request.app["control_facade"]
    if facade is None:
        return _json_error(
            503,
            ControlError(
                code="facade_unavailable",
                message="Control facade is not wired on this server",
            ),
        )

    try:
        raw_body = await request.read()
        payload: dict[str, Any] = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return _json_error(
            400,
            ControlError(
                code="malformed_json",
                message=f"Request body is not valid JSON: {exc}",
            ),
        )
    if not isinstance(payload, dict):
        return _json_error(
            400,
            ControlError(
                code="invalid_request",
                message="Request body must be a JSON object",
            ),
        )

    try:
        send_request = SendRequest.model_validate(payload)
    except ValidationError as exc:
        errors = exc.errors()
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in errors
        )
        return _json_error(
            400,
            ControlError(
                code="validation_error",
                message=detail or "Request validation failed",
            ),
        )

    try:
        result: SendResult = await facade.send(send_request)
    except ControlFacadeError as exc:
        return _json_error(exc.status, exc.error)
    except Exception as exc:
        status, error = _control_error_from_exception(exc)
        return _json_error(status, error)

    return web.json_response(result.model_dump(mode="json", exclude_none=True))


def register_control_routes(app: web.Application, facade: BotControlFacade) -> None:
    """Register control routes on *app* and store the facade for handlers.

    Called by :meth:`WebUIServer.set_control_facade` (and the test harness)
    so the route handler can access the facade via ``app["control_facade"]``.
    """
    app["control_facade"] = facade
    app.router.add_post(CONTROL_HISTORY_PATH, handle_history)
    app.router.add_post(CONTROL_SEND_PATH, handle_send)


__all__ = [
    "CONTROL_HISTORY_PATH",
    "CONTROL_SEND_PATH",
    "handle_history",
    "handle_send",
    "register_control_routes",
]
