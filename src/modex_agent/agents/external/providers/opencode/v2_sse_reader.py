"""``OpenCodeV2SseReader`` — persistent SSE reader with per-session demux.

Long-lived connection to ``/api/event`` with per-session event demux,
``durable.seq`` replay, stall reconnect, and ``restart()``.

The ``/api/event`` stream carries BOTH V2 events (``session.next.*``, payload
in ``data``) and V1 events (``message.part.*``, ``session.created``, payload
in ``properties``). The reader normalizes both envelope shapes before
dispatching to the parser.

Child session auto-discovery: when a ``session.created`` event arrives with
``info.parentID`` matching a registered session, the child session is
registered with its OWN callback, obtained from the parent registration's
child-callback factory. Core turn events carry no session identity, so this
per-session callback demux is the single place a provider child session
attaches to its delivery target — the normalizer on the other side of the
factory owns the routing (child sink creation, lineage registration).

Events for sessions with neither a registered callback nor a discoverable
parent factory are dropped.

Permission and question events are NOT handled here. The spawn env sets
``OPENCODE_PERMISSION='{"*":"allow","question":"deny"}'`` which prevents
``permission.asked`` from firing and removes the question tool at registration.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import aiohttp

from ...transports.abc import ChildTurnEventCallbackFactory, TurnEventCallback
from .session_state import OpenCodeSessionState, SessionActivity
from .v2_parser import OpenCodeV1EventType, OpenCodeV2EventParser

logger = logging.getLogger(__name__)

__all__ = ["OpenCodeV2SseReader"]

_STALL_TIMEOUT: float = 20.0
_RECONNECT_DELAY: float = 0.25
_REPLAY_STALL_TIMEOUT: float = 0.5


def _extract_parent_sid(data: dict[str, Any]) -> str | None:
    """Extract ``parentID`` from a ``session.created`` event's normalized data.

    Shared by the dual-path registry dispatch and ``_maybe_discover_child``
    (design 5.4: "建议把 parentID 提取收敛成一个函数"). Returns the parentID
    string when ``data.info.parentID`` (or top-level ``data.parentID``) is a
    non-empty string, else ``None``.
    """
    info = data.get("info", data)
    if isinstance(info, dict):
        parent_id = info.get("parentID")
        if isinstance(parent_id, str) and parent_id:
            return parent_id
    return None


def _activity_from_event(payload: dict[str, Any]) -> SessionActivity | None:
    """Map a ``session.status``/``session.idle``/``session.error`` event to activity.

    ``session.status`` carries ``status.type`` (``busy``/``idle``/``retry``);
    ``retry`` maps to BUSY (the session is actively retrying, e.g. rate-limit
    backoff). The deprecated ``session.idle`` event maps to IDLE;
    ``session.error`` maps to ERROR. Returns ``None`` for all other event
    types (the registry ignores them).
    """
    event_type = payload.get("type")
    if event_type == OpenCodeV1EventType.SESSION_STATUS:
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload.get("properties")
        if not isinstance(data, dict):
            return None
        status = data.get("status")
        if not isinstance(status, dict):
            return None
        status_type = status.get("type")
        if status_type in ("busy", "retry"):
            return SessionActivity.BUSY
        if status_type == "error":
            return SessionActivity.ERROR
        return SessionActivity.IDLE
    if event_type == OpenCodeV1EventType.SESSION_IDLE:
        return SessionActivity.IDLE
    if event_type == OpenCodeV1EventType.SESSION_ERROR_V1:
        return SessionActivity.ERROR
    return None


class _ServerUnavailableError(Exception):
    """Internal signal: server is down (connection refused)."""


class OpenCodeV2SseReader:
    """Persistent SSE reader for ``/api/event`` with per-session demux.

    The reader owns the SSE connection lifecycle: connect → consume →
    (stall/close) → replay missed durable events → reconnect. Per-session
    callbacks receive core ``TurnEvent`` objects.

    Child sessions are auto-registered when ``session.created`` events with
    a matching ``parentID`` are seen — no manual registration needed, as
    long as the parent was registered with a child-callback factory.
    """

    def __init__(self, server_url: str, workdir: str, parser: OpenCodeV2EventParser) -> None:
        self._server_url = server_url.rstrip("/")
        self._workdir = workdir
        self._parser = parser

        self._stopped = True
        self._server_unavailable = False

        self._session_callbacks: dict[str, TurnEventCallback] = {}
        self._child_factories: dict[str, ChildTurnEventCallbackFactory] = {}
        self._last_known_seq: dict[str, int] = {}
        self._seen_event_ids: dict[str, set[str]] = {}
        self._child_to_parent: dict[str, str] = {}

        self._http_session: aiohttp.ClientSession | None = None
        self._owns_http_session = False
        self._reconnect_task: asyncio.Task[None] | None = None
        self._stall_timeout: float = _STALL_TIMEOUT
        self._reconnect_delay: float = _RECONNECT_DELAY

        self._session_state: OpenCodeSessionState | None = None

    # -- Public API --------------------------------------------------------

    def attach_session_state(self, state: OpenCodeSessionState) -> None:
        """Attach the shared session-state registry for dual-path dispatch.

        Once attached, every raw SSE event is fed to ``state.on_event``
        before the parser path runs (design 5.4). The registry is
        orthogonal to ``register_session``/``unregister_session`` (which
        route output events) — it drives turn-completion detection.
        """
        self._session_state = state

    async def start(self) -> None:
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._stopped = False
        self._server_unavailable = False
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()
            self._owns_http_session = True
        self._reconnect_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopped = True
        task = self._reconnect_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._reconnect_task = None
        if (
            self._owns_http_session
            and self._http_session is not None
            and not self._http_session.closed
        ):
            await self._http_session.close()
        self._http_session = None
        self._owns_http_session = False

    async def restart(self, server_url: str) -> None:
        if self._session_state is not None:
            self._session_state.mark_reconnect_pending()
        await self.stop()
        self._server_url = server_url.rstrip("/")
        self._last_known_seq.clear()
        self._seen_event_ids.clear()
        self._child_to_parent.clear()
        for sid in self._session_callbacks:
            self._seen_event_ids[sid] = set()
        self._server_unavailable = False
        await self.start()

    def register_session(
        self,
        session_id: str,
        on_event: TurnEventCallback,
        on_child_event: ChildTurnEventCallbackFactory | None = None,
    ) -> None:
        """Route one provider session's events to ``on_event``.

        ``on_child_event`` (when supplied) is the factory used to obtain
        delivery targets for provider sessions minted under this one at
        runtime (subagent child sessions). Registrations without a factory
        do not observe child sessions — child events are dropped.
        """
        self._session_callbacks[session_id] = on_event
        self._seen_event_ids.setdefault(session_id, set())
        if on_child_event is not None:
            self._child_factories[session_id] = on_child_event
        else:
            self._child_factories.pop(session_id, None)

    def unregister_session(self, session_id: str) -> None:
        self._session_callbacks.pop(session_id, None)
        self._child_factories.pop(session_id, None)
        self._seen_event_ids.pop(session_id, None)
        for child, parent in list(self._child_to_parent.items()):
            if parent == session_id:
                self._session_callbacks.pop(child, None)
                self._seen_event_ids.pop(child, None)
                self._child_to_parent.pop(child, None)

    # -- Reconnect loop ----------------------------------------------------

    async def _run_loop(self) -> None:
        while not self._stopped:
            if self._server_unavailable:
                await asyncio.sleep(0.1)
                continue
            try:
                await self._connect_and_consume()
            except _ServerUnavailableError:
                self._server_unavailable = True
                logger.warning("OpenCode server unavailable — waiting for restart()")
                continue
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("OpenCode SSE stream error")
            if self._stopped or self._server_unavailable:
                continue
            await self._replay_missed_events()
            await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_consume(self) -> None:
        session = self._http_session
        if session is None:
            raise _ServerUnavailableError()
        # /event carries BOTH V1 (message.part.*, session.created) and V2
        # (session.next.*) events. /api/event only carries V2 events. Since
        # prompt dispatch uses V1 prompt_async (for task tool support), V1
        # events must be captured — so /event is the correct SSE endpoint.
        url = f"{self._server_url}/event"
        headers = {"Accept": "text/event-stream", "x-opencode-directory": self._workdir}
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise _ServerUnavailableError()
                await self._consume_stream(resp)
        except aiohttp.ClientConnectorError as exc:
            raise _ServerUnavailableError() from exc

    async def _consume_stream(self, resp: aiohttp.ClientResponse) -> None:
        while not self._stopped:
            try:
                raw = await asyncio.wait_for(resp.content.readline(), timeout=self._stall_timeout)
            except TimeoutError:
                logger.warning("OpenCode SSE stall — reconnecting")
                return
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            json_str = line[len("data:") :].strip()
            if json_str:
                await self._process_event(json_str)

    # -- Event processing --------------------------------------------------

    async def _process_event(self, json_str: str) -> None:
        try:
            payload: dict[str, Any] = json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            return
        if not isinstance(payload, dict):
            return

        # Normalize envelope: V2 uses "data", V1 uses "properties".
        data = payload.get("data")
        if not isinstance(data, dict):
            data = payload.get("properties")
        if not isinstance(data, dict):
            return

        sid_raw = data.get("sessionID")
        sid_str = sid_raw if isinstance(sid_raw, str) and sid_raw else None

        self._track_durable_seq(payload, sid_str)
        self._maybe_discover_child(payload, data, sid_str)

        # Feed the registry before dedup — it needs all events (incl. dups)
        # for state tracking; on_event is idempotent for status events.
        if self._session_state is not None:
            self._session_state.on_event(
                sid_str,
                payload.get("type") or "",
                parent_sid=_extract_parent_sid(data),
                activity=_activity_from_event(payload),
            )

        # Dedup
        event_id = payload.get("id")
        if isinstance(event_id, str) and sid_str is not None:
            seen = self._seen_event_ids.get(sid_str)
            if seen is not None:
                if event_id in seen:
                    return
                seen.add(event_id)

        for event in self._parser.parse_line(json_str):
            callback = self._callback_for_session(sid_str)
            if callback is not None:
                await callback(event)

    def _callback_for_session(self, sid: str | None) -> TurnEventCallback | None:
        """Resolve the delivery target for one provider session id.

        Direct registration wins; otherwise a known child→parent link
        resolves through the parent's child-callback factory (registering
        the child for subsequent events). Sessions with no resolvable
        target are dropped.
        """
        if sid is None:
            return None
        callback = self._session_callbacks.get(sid)
        if callback is not None:
            return callback
        parent = self._child_to_parent.get(sid)
        if parent is None:
            return None
        factory = self._child_factories.get(parent)
        if factory is None:
            return None
        callback = factory(sid)
        self._session_callbacks[sid] = callback
        self._seen_event_ids.setdefault(sid, set())
        return callback

    def _maybe_discover_child(
        self, payload: dict[str, Any], data: dict[str, Any], sid: str | None
    ) -> None:
        """Auto-register child sessions from ``session.created`` events."""
        if payload.get("type") != OpenCodeV1EventType.SESSION_CREATED:
            return
        parent_id = _extract_parent_sid(data)
        if parent_id is None:
            return
        if parent_id not in self._session_callbacks:
            return
        info = data.get("info", data)
        if not isinstance(info, dict):
            return
        child_id = info.get("id")
        if not (isinstance(child_id, str) and child_id and child_id not in self._session_callbacks):
            return
        factory = self._child_factories.get(parent_id)
        if factory is None:
            # The parent's registration observes no child sessions —
            # leave the child unregistered (its events are dropped).
            return
        self._session_callbacks[child_id] = factory(child_id)
        self._seen_event_ids.setdefault(child_id, set())
        self._child_to_parent[child_id] = parent_id
        logger.info("Auto-discovered child session %s (parent=%s)", child_id, parent_id)

    def _track_durable_seq(self, payload: dict[str, Any], sid: str | None) -> None:
        if sid is None:
            return
        durable = payload.get("durable")
        if not isinstance(durable, dict):
            return
        seq = durable.get("seq")
        if isinstance(seq, int) and seq > self._last_known_seq.get(sid, 0):
            self._last_known_seq[sid] = seq

    # -- Replay ------------------------------------------------------------

    async def _replay_missed_events(self) -> None:
        for session_id, last_seq in list(self._last_known_seq.items()):
            if session_id in self._session_callbacks and last_seq > 0:
                await self._replay_session(session_id, last_seq)

    async def _replay_session(self, session_id: str, after_seq: int) -> None:
        http_session = self._http_session
        if http_session is None:
            return
        url = f"{self._server_url}/api/session/{session_id}/event"
        headers = {"Accept": "text/event-stream", "x-opencode-directory": self._workdir}
        params = {"after": str(after_seq)}
        try:
            async with http_session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    return
                while not self._stopped:
                    try:
                        raw = await asyncio.wait_for(
                            resp.content.readline(), timeout=_REPLAY_STALL_TIMEOUT
                        )
                    except TimeoutError:
                        return
                    if not raw:
                        return
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line or line.startswith(":") or not line.startswith("data:"):
                        continue
                    json_str = line[len("data:") :].strip()
                    if json_str:
                        await self._process_event(json_str)
        except (aiohttp.ClientConnectorError, TimeoutError):
            return
        except Exception:  # noqa: BLE001
            logger.exception("OpenCode replay error for session %s", session_id)
