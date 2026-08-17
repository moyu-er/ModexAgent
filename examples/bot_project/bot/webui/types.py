"""Shared types, constants, and helpers extracted from :mod:`bot.webui.server`.

This module is a leaf dependency: it MUST NOT import from ``bot.webui.server``.
``server.py`` re-exports every symbol below so existing
``from bot.webui.server import X`` imports continue to work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from aiohttp import web

from bot.adapters.web_socket import WebSocketInputAdapter
from bot.webui.events import ServerEvent
from modex_agent.core.session_id import SessionInfo
from modex_graph import GraphOutput

logger = logging.getLogger(__name__)


async def _safe_send_json(ws: web.WebSocketResponse, data: dict[str, object]) -> None:
    """Send JSON to *ws*, swallowing errors from a closed/broken connection.

    Used for fire-and-forget notifications (attached/error/conversation_deleted)
    so a send failure does not leak as an unretrieved asyncio task exception.
    The failure is logged so it can be diagnosed if it occurs unexpectedly.
    """
    try:
        await ws.send_json(data)
    except (ConnectionError, RuntimeError) as exc:
        # Connection already closed or message serialisation impossible; the
        # main WebSocket loop will detect the close and clean up.
        logger.warning("WebSocket send_json failed: %s", exc)


@dataclass
class _GraphSubscription:
    """One (connection, graph instance) event subscription.

    Holds the workspace subscriber-registry reference and this connection's
    own queue so teardown can deregister the queue without server access.
    ``registry`` is the workspace's ``graph_event_subscribers`` dict (the same
    object the ``WebUIGraphOutputAdapter`` fans out to).
    """

    instance_id: int
    registry: dict[int, list[asyncio.Queue[GraphOutput]]]
    queue: asyncio.Queue[GraphOutput]
    task: asyncio.Task[None]


@dataclass
class _WsConnectionState:
    """Tracks all sessions and forward tasks bound to one WebSocket connection."""

    attached_sessions: list[str] = field(default_factory=list)
    forward_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    # Graph event subscriptions (subscribe_graph action), keyed by instance
    # id. Orthogonal to attached_sessions: re-attaching a conversation does
    # NOT clear them (cleanup callers pass include_graphs=False on attach).
    graph_subscriptions: dict[int, _GraphSubscription] = field(default_factory=dict)
    # Set by cleanup() before cancelling tasks so the queue watcher stops
    # appending sessions / spawning forward tasks that would escape cancellation.
    _stopped: bool = False

    @property
    def subscribed_graphs(self) -> list[int]:
        """Graph instance ids this connection is currently subscribed to."""
        return list(self.graph_subscriptions)

    async def cleanup_graph_subscriptions(self) -> None:
        """Deregister every graph subscription owned by this connection.

        Cancels each forward task and removes its queue from the workspace
        subscriber registry, so a closed/unsubscribed connection never leaves
        a queue behind for the adapter to fan out to.
        """
        for sub in self.graph_subscriptions.values():
            queues = sub.registry.get(sub.instance_id)
            if queues is not None and sub.queue in queues:
                queues.remove(sub.queue)
            if not sub.task.done():
                sub.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sub.task
        self.graph_subscriptions.clear()

    async def cleanup(
        self,
        input_adapter: WebSocketInputAdapter,
        ws: object,
        *,
        include_graphs: bool = True,
    ) -> None:
        """Drain queues, cancel forward tasks, and unregister all sessions.

        Only THIS connection's per-session queues are drained/unregistered —
        with multicast queues, other connections attached to the same
        sessions (duplicate tabs) keep their streams untouched.

        ``include_graphs=False`` is used by the ATTACH handler: switching
        conversations must not clear graph subscriptions (the two lifecycles
        are orthogonal). The disconnect path uses the default and tears down
        sessions and graph subscriptions together.
        """
        # Signal the queue watcher to stop BEFORE cancelling tasks so it does
        # not append a new session / spawn a forward task between our clear()
        # and task cancellation (which would orphan that task forever).
        self._stopped = True
        # Drain pending deltas first so a cancelling forward task cannot consume
        # messages intended for an old session and forward them to a reused
        # WebSocket connection during re-attach.
        for session_id in self.attached_sessions:
            q = input_adapter.get_delta_queue(session_id, ws)
            if q is not None:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
        for task in self.forward_tasks:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.forward_tasks.clear()
        for session_id in self.attached_sessions:
            input_adapter.unregister_connection(session_id, ws)
        self.attached_sessions.clear()
        if include_graphs:
            await self.cleanup_graph_subscriptions()


# ── Constants ──────────────────────────────────────────────────────────────

_DEFAULT_AGENT_NAME: str = "main"
_API_SESSIONS_PATH: str = "/api/sessions"
_API_SESSIONS_SESSION_PATH: str = "/api/sessions/{session_id}"
_API_MODELS_PATH: str = "/api/models"
_API_MEDIA_CONFIG_PATH: str = "/api/media/config"
_WS_PATH: str = "/ws"
_WEBUI_STATIC_PREFIX: str = "/webui/"
_DEFAULT_STATIC_DIST: Path = Path(__file__).resolve().parent.parent / "web" / "dist"

# Skill upload size caps. Per-file and total limits protect the server from
# oversized uploads; both are simple constants (not configurable per-pool — a
# skill is global, so a single pair of limits is sufficient).
_SKILL_MAX_FILE_MB: int = 20
_SKILL_MAX_TOTAL_MB: int = 100
_SKILL_MAX_FILE_BYTES: int = _SKILL_MAX_FILE_MB * 1024 * 1024
_SKILL_MAX_TOTAL_BYTES: int = _SKILL_MAX_TOTAL_MB * 1024 * 1024


class _SkillUploadFallback(Exception):
    """Internal sentinel: multipart upload unavailable, fall back to JSON."""


@dataclass(frozen=True)
class RuntimeStores:
    """Backend-aware runtime stores resolved for one workspace + pool.

    Carries the ``TodoStore`` and ``TurnStateStore`` the WebUI endpoints
    should read from, matching the backend the agent writes to. ``None``
    fields signal the endpoint to fall back to its hardcoded file store.
    """

    todo_store: Any = None
    turn_store: Any = None


def _skill_relpath(filename: str) -> str | None:
    """Normalize an uploaded skill filename to a path relative to ``<skillName>/``.

    The frontend uploads with ``webkitdirectory``, so filenames look like
    ``mySkill/SKILL.md`` or ``mySkill/sub/f.txt``. We strip the leading
    ``<skillName>/`` segment so the resulting key is relative to the skill
    root. Bare filenames (no slash) are kept as-is. Returns ``None`` for
    traversal attempts (``..`` segments).
    """
    import os
    from urllib.parse import unquote

    # aiohttp may deliver filenames URL-encoded (e.g. ``greeter%2FSKILL.md``);
    # decode first so the path-segment logic below sees real slashes.
    cleaned = unquote(filename).replace("\\", "/")
    # Drop a leading drive letter (Windows) and any leading slashes.
    if len(cleaned) >= 2 and cleaned[1] == ":":
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    if "/" in cleaned:
        # Drop the first segment (the skill-name prefix from webkitdirectory).
        cleaned = cleaned.split("/", 1)[1] if "/" in cleaned else cleaned
    if not cleaned or cleaned.startswith("/"):
        return None
    norm = os.path.normpath(cleaned).replace("\\", "/")
    parts = norm.split("/")
    if any(p in {".."} for p in parts):
        return None
    return norm


# Multipart upload read chunk. Large enough to amortize per-chunk overhead on a
# 20 MB image, small enough that the size pre-check fires promptly.
_UPLOAD_CHUNK_BYTES: int = 64 * 1024


@dataclass
class SessionListEntry:
    """One row of the ``GET /api/sessions`` response.

    A typed view over :class:`SessionInfo` plus the resolved pool name, so the
    session list API serializes a structure rather than a loose dict.
    """

    session_id: str
    agent_name: str
    pool: str
    parent_session_id: str | None
    created_at: int | None
    updated_at: int | None
    metadata: dict[str, Any]


def _entry_from_session(session: SessionInfo, pool: str) -> SessionListEntry:
    """Build a :class:`SessionListEntry` from a stored session + its pool."""
    return SessionListEntry(
        session_id=session.session_id,
        agent_name=session.agent_name,
        pool=pool,
        parent_session_id=session.parent_session_id,
        created_at=session.created_at,
        updated_at=session.updated_at,
        metadata=session.metadata,
    )


def _new_uuid_prefix() -> str:
    """Generate a new 12-char uuid prefix for a session_id."""
    return uuid4().hex[:12]


def _materialize_partial_deltas(
    events: list[ServerEvent], agent_name: str
) -> dict[str, object] | None:
    """Fold partial streaming delta events into a synthetic streaming assistant_turn.

    Deltas are grouped by ``segment_id`` (empty string → anonymous segment)
    and merged append-wise per group, preserving order of first appearance.
    The result carries ``is_streaming=True`` so the frontend renders it as an
    in-progress message and continues appending live WS deltas on top.
    """
    from bot.webui.events import ModelContentDelta, ModelReasoningDelta

    segment_order: list[str] = []
    segment_text: dict[str, str] = {}
    segment_kind: dict[str, str] = {}
    first_ts: int | None = None
    turn_id: str = ""

    for evt in events:
        if first_ts is None or evt.timestamp < first_ts:
            first_ts = evt.timestamp
        if not turn_id:
            tid = getattr(evt, "turn_id", "")
            if tid:
                turn_id = tid
        if isinstance(evt, ModelContentDelta):
            seg = evt.segment_id or "_text"
        elif isinstance(evt, ModelReasoningDelta):
            seg = evt.segment_id or "_reasoning"
        else:
            continue
        kind = "reasoning" if isinstance(evt, ModelReasoningDelta) else "text"
        if seg not in segment_text:
            segment_text[seg] = ""
            segment_kind[seg] = kind
            segment_order.append(seg)
        segment_text[seg] += evt.text

    if not segment_order:
        return None

    blocks: list[dict[str, object]] = []
    for seg in segment_order:
        kind = segment_kind[seg]
        text = segment_text[seg]
        if text:
            blocks.append({"kind": kind, "text": text})

    if not blocks:
        return None

    return {
        "event": "assistant_turn",
        "session_id": "",
        "agent_name": agent_name,
        "timestamp": first_ts or 0,
        "turn_id": turn_id,
        "blocks": blocks,
        "latency_ms": 0,
        "is_streaming": True,
    }


# ── Workspace membership seam (owned by the consumer) ──────────────────────


class WorkspaceIndex(ABC):
    """Workspace- and pool-partitioned transcript access the WebUI needs.

    Implemented by the business layer
    (:class:`bot.service.workspace_store.WorkspaceScopedTranscriptStore`).
    Defined here so the WebUI does not depend on ``bot.service`` (dependency
    direction stays service → webui).
    """

    @abstractmethod
    async def list_sessions(self, sessions_dir: Path) -> set[str]:
        """Return all session ids under *sessions_dir*."""
        ...

    @abstractmethod
    async def list_sessions_by_prefix(
        self, session_prefix: str, sessions_dir: Path | None = None
    ) -> set[str]:
        """Return matching session ids under *sessions_dir*."""
        ...

    @abstractmethod
    async def last_updated(self, session_id: str, sessions_dir: Path | None = None) -> int | None:
        """Return the latest transcript timestamp for *session_id*."""
        ...


# ── Workspace picker script (Windows-only fallback) ────────────────────────

_PICKER_TIMEOUT_S = 600

_PICKER_SCRIPT = """\
import ctypes
import platform
import sys

if platform.system() == "Windows":
    try:
        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(mustexist=True)
if path:
    sys.stdout.write(path)
root.destroy()
"""
