"""aiohttp WebUI server with REST API and WebSocket support.

Workspace handling:
  - Transcripts live in ONE shared flat store, the same store the agent
    emitter and IM FanIn write to.  The framework is NOT workspace-aware.
  - Workspace is a pure backend-service concern.  Session→workspace attribution
    is owned by :class:`bot.service.workspace_store.WorkspaceScopedTranscriptStore`,
    which the WebUI consumes through the :class:`WorkspaceIndex` interface defined
    here (dependency direction stays service → webui).  When no index is
    injected (e.g. basic tests), all main-agent sessions are listed unfiltered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout, web
from pydantic import ValidationError

from bot.adapters.channels import set_conv_channel
from bot.adapters.web_socket import WebSocketInputAdapter
from bot.config.prompt_store import PromptExistsError
from bot.service.config_controller import ConfigController, FieldValidationError
from bot.service.model_config import BotModelConfig, ProviderCfg
from bot.service.pool_config_controller import (
    PoolConfigController,
    PoolNotEmptyError,
    PromptInUseError,
)
from bot.webui.events import (
    DeltaEnvelope,
    ServerEvent,
    WebSocketAction,
    WebUIEventType,
)
from bot.webui.model_fetch import FetchModelsReq, ModelFetchError, fetch_provider_models
from bot.webui.transcript_store import TranscriptStore
from modex_agent.core.constants import InterfaceFormat
from modex_agent.core.session_id import (
    SessionIdFactory,
    SessionInfo,
    agent_of,
    session_id_prefix_of,
)
from modex_agent.core.session_store import SessionStore
from modex_agent.core.types import TodoStatus
from modex_agent.runtime.store import JsonFileTodoStore
from modex_agent.utils.timezone import get_user_timezone
from modex_agent.workspace.paths import WorkspacePaths
from modex_agent.workspace.port import WorkspaceControlPort
from modex_agent.workspace.runtime import resolve_workspace_root

if TYPE_CHECKING:
    from bot.service.session_gc import SessionGarbageCollector

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
class _WsConnectionState:
    """Tracks all sessions and forward tasks bound to one WebSocket connection."""

    attached_sessions: list[str] = field(default_factory=list)
    forward_tasks: list[asyncio.Task[None]] = field(default_factory=list)
    # Set by cleanup() before cancelling tasks so the queue watcher stops
    # appending sessions / spawning forward tasks that would escape cancellation.
    _stopped: bool = False

    async def cleanup(self, input_adapter: WebSocketInputAdapter) -> None:
        """Drain queues, cancel forward tasks, and unregister all sessions."""
        # Signal the queue watcher to stop BEFORE cancelling tasks so it does
        # not append a new session / spawn a forward task between our clear()
        # and task cancellation (which would orphan that task forever).
        self._stopped = True
        # Drain pending deltas first so a cancelling forward task cannot consume
        # messages intended for an old session and forward them to a reused
        # WebSocket connection during re-attach.
        for session_id in self.attached_sessions:
            q = input_adapter.get_delta_queue(session_id)
            if q is not None:
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
        for task in self.forward_tasks:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self.forward_tasks.clear()
        for session_id in self.attached_sessions:
            input_adapter.unregister_connection(session_id)
        self.attached_sessions.clear()


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


# ── Server ─────────────────────────────────────────────────────────────────


class WebUIServer:
    """HTTP + WebSocket server for the bot WebUI.

    Transcripts are stored in ONE shared flat store (the same store the agent
    pipeline writes to).  Workspace is a pure backend-service concept: the
    server filters/listens to session→workspace attribution through an injected
    :class:`WorkspaceIndex`.  The framework (emitter / agents) is never
    workspace-aware.
    """

    def __init__(
        self,
        input_adapter: WebSocketInputAdapter,
        transcript_store: TranscriptStore,
        static_dist: Path | None = None,
        data_dir: Path | None = None,
        home_sessions_dir: Path | None = None,
    ) -> None:
        self._input: WebSocketInputAdapter = input_adapter
        # Shared flat transcript store -- same store the agent emitter and IM
        # FanIn write to.  All transcript I/O (read + write) goes through it.
        self._store: TranscriptStore = transcript_store
        self._static_dist: Path | None = static_dist
        self._data_dir: Path | None = data_dir
        self._home_sessions_dir: Path = (
            home_sessions_dir if home_sessions_dir is not None else Path()
        )
        self._data_dir_name: str = ""

        # Session store (WorkspacePoolSessionStore) -- injected by WebUIService.
        # Either a single store (tests, single-workspace) or a factory that
        # builds a fresh store per workspace index_dir (production multi-live).
        self._session_store: SessionStore | None = None
        self._session_store_factory: Callable[[Path], Awaitable[SessionStore]] | None = None
        # SessionIdFactory -- injected by WebUIService for creating new sessions.
        self._session_factory: SessionIdFactory | None = None

        # Workspace control -- injected by WebUIService for the workspace API.
        # A per-conversation WorkspaceControlPort. The HTTP workspace API is
        # single-active (the browser's current workspace), so it drives the
        # port under a global sentinel conversation id.
        self._workspace_control: WorkspaceControlPort | None = None
        # Workspace+pool partition index -- injected by WebUIService.  When None,
        # the flat shared store is used (workspace-agnostic, basic tests).
        self._workspace_index: WorkspaceIndex | None = None
        # Pool metadata -- injected by WebUIService after pool initialization.
        self._pool_agent_names: list[str] = [_DEFAULT_AGENT_NAME]
        self._pool_switch_callback: Callable[[str, str], None] | None = None
        self._pool_resolver: Callable[[str], str | None] | None = None
        self._agent_resolver: Callable[[str], str] | None = None
        self._agent_pool_map: dict[str, str] = {}
        self._recent_workspaces = None  # set by WebUIService
        self._input_pipeline = None  # injected by WebUIService
        self._input_ctx = None
        # Loader that re-reads config/model.yml on each GET /api/models so the
        # selector reflects CLI edits (e.g. `modexbot model`) without a restart.
        # Runtime routing still requires restart (CLI prints "restart to apply").
        self._model_config_loader: Callable[[], BotModelConfig | None] | None = None
        # ConfigController -- injected by WebUIService; serves /api/config/{domain}
        # and /api/system/restart. None degrades the endpoints to 503.
        self._config_controller: ConfigController | None = None
        # PoolConfigController -- injected by WebUIService; serves /api/pools,
        # /api/mcp, /api/skills and the per-agent prompt/skills sub-routes. None
        # degrades the endpoints to 503 (matches ConfigController convention).
        self._pool_config_controller: PoolConfigController | None = None
        # SessionGarbageCollector -- injected by WebUIService for cascade
        # session deletion. None until wired (handler delegation is separate).
        self._session_gc = None
        # Backend-aware runtime store resolver: ``async callback(ws_root, pool)
        # -> RuntimeStores``. Injected by WebUIService so the todos/approvals
        # endpoints read from the same backend the agent writes to.
        self._store_resolver: Callable[[Path, str], Awaitable[RuntimeStores]] | None = None

        # Lazy-shared aiohttp ClientSession for outbound provider model-list
        # fetches. Created on first use; closed on app shutdown.
        self._http_session: ClientSession | None = None

        self.app = web.Application()
        self._setup_routes()
        self.app.on_cleanup.append(self._close_http_session)

    # ------------------------------------------------------------------
    # Workspace helpers
    # ------------------------------------------------------------------

    def _ws_root_of(self, ws_raw: str) -> Path:
        """Resolve a ws ("ws" == workspace) value to its ROOT directory.

        Single source of truth for workspace-root resolution, shared by every
        read AND write path (session index, transcript sessions dir, the
        pipeline's bound workspace root) so a message written under a workspace
        is always read back from the same workspace.

        - Empty string -> home workspace root (canonical home).
        - Relative path -> resolved against the home workspace root.
        - Absolute path -> used as-is.
        Falls back to the home root on any resolution error.

        Note: the ``_sessions_dir_of_ws`` / ``_index_dir_of_ws`` readers
        short-circuit home to the precomputed ``_home_sessions_dir`` so home
        never depends on ``_data_dir_name`` being set; this method is the
        fallback for the home ROOT (e.g. the pipeline's bound workspace root).
        """
        home_root = self._home_sessions_dir.parent.parent
        if not ws_raw:
            return home_root
        base = Path(ws_raw).expanduser()
        if not base.is_absolute() and self._workspace_control is not None:
            base = self._workspace_control.home / base
        try:
            return base.resolve(strict=False)
        except (OSError, ValueError) as exc:
            logger.warning("Failed to resolve workspace path %r: %s", ws_raw, exc)
            return home_root

    def _sessions_dir_of_ws(self, ws_raw: str) -> Path:
        """Resolve the raw ws path to the sessions directory (transcripts).

        Home (empty ``ws_raw``) returns the canonical ``_home_sessions_dir``;
        a non-home workspace resolves to ``<root>/<data_dir>/sessions``.
        """
        from modex_agent.workspace.paths import WorkspacePaths

        if not ws_raw:
            return self._home_sessions_dir
        try:
            return WorkspacePaths(root=self._ws_root_of(ws_raw) / self._data_dir_name).sessions_dir
        except (OSError, ValueError) as exc:
            logger.warning("Failed to build sessions dir for %r: %s", ws_raw, exc)
            return self._home_sessions_dir

    def _index_dir_of_ws(self, ws_raw: str) -> Path:
        """Resolve the raw ws path to the session-INDEX directory.

        Mirrors :meth:`_sessions_dir_of_ws` but for the ``session_index`` layer,
        so the session index is read/written per-workspace (no cross-ws leakage).
        """
        from modex_agent.workspace.paths import WorkspacePaths

        home_index = WorkspacePaths(root=self._home_sessions_dir.parent).session_index_dir
        if not ws_raw:
            return home_index
        try:
            return WorkspacePaths(
                root=self._ws_root_of(ws_raw) / self._data_dir_name
            ).session_index_dir
        except (OSError, ValueError) as exc:
            logger.warning("Failed to build session-index dir for %r: %s", ws_raw, exc)
            return home_index

    def _media_dir_of_ws(self, ws_raw: str, pool: str) -> Path:
        """Resolve the raw ws path to the pool's MEDIA directory.

        Mirrors :meth:`_sessions_dir_of_ws` and the business
        :class:`WorkspaceScopedMediaStore` ctxvar resolution
        (``WorkspacePaths(root=<ws_root>/<data_dir>).media_dir(pool)``) so an
        inbound attachment written under a workspace is read back from the same
        workspace's media dir. Home (empty ``ws_raw``) resolves against the
        precomputed ``_home_sessions_dir`` parent so it never depends on
        ``_data_dir_name`` being set, exactly like the sessions reader.
        """
        if not ws_raw:
            return WorkspacePaths(root=self._home_sessions_dir.parent).media_dir(pool)
        try:
            return WorkspacePaths(root=self._ws_root_of(ws_raw) / self._data_dir_name).media_dir(
                pool
            )
        except (OSError, ValueError) as exc:
            logger.warning("Failed to build media dir for %r: %s", ws_raw, exc)
            return WorkspacePaths(root=self._home_sessions_dir.parent).media_dir(pool)

    def _media_tmp_dir_of_ws(self, ws_raw: str, pool: str) -> Path:
        """Resolve the raw ws path to the pool's media ``_tmp`` directory.

        Staging area for the upload endpoint (:meth:`_handle_upload_attachment`)
        — accepted files are re-persisted by the ingest stage into the real
        media dir, so temp files here are disposable. Resolved the same way as
        :meth:`_media_dir_of_ws` so a temp file written under a workspace is
        read back from the same workspace when the WS message flows through the
        pipeline. Leftover files from a previous run are reclaimed by
        :meth:`sweep_media_tmp_orphans` at startup.
        """
        return self._media_dir_of_ws(ws_raw, pool) / "_tmp"

    def sweep_media_tmp_orphans(self) -> None:
        """Delete leftover upload temp files from a previous run.

        The upload endpoint stages bytes under ``<data_dir>/media/<pool>/_tmp``;
        the ingest stage re-persists accepted files into the real media dir
        (``uploads/``). A file left in ``_tmp`` is an upload that never became a
        WS message (client disconnected, crash, etc.) — disposable. Sweep ``_tmp``
        across home + every recent workspace at startup so orphans do not
        accumulate on disk. Accepted files under ``uploads/`` are never touched.
        """
        for data_root in self._known_workspace_data_roots():
            media_dir = data_root / "media"
            if not media_dir.is_dir():
                continue
            # Only ``_tmp`` dirs one level under ``media/<pool>/`` — leaves the
            # ``uploads/`` subtree (accepted, budget-managed bytes) untouched.
            for tmp_dir in media_dir.glob("*/_tmp"):
                if tmp_dir.is_dir():
                    self._clear_dir_contents(tmp_dir)

    @staticmethod
    def _clear_dir_contents(path: Path) -> None:
        """Remove every entry inside *path*, keeping the directory itself."""
        for entry in path.iterdir():
            try:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
            except OSError as exc:
                logger.warning("media/_tmp sweep: could not remove %s: %s", entry, exc)

    def _known_workspace_data_roots(self) -> list[Path]:
        """Distinct ``<root>/<data_dir>`` dirs for home + recent workspaces.

        Home's data root is ``_home_sessions_dir.parent`` (already encodes the
        data dir, so it resolves even before ``_data_dir_name`` is set). Each
        recent workspace resolves via :meth:`_ws_root_of` + ``_data_dir_name``;
        recent is skipped while ``_data_dir_name`` is unset (minimal test wiring).
        """
        roots: list[Path] = [self._home_sessions_dir.parent]
        if self._data_dir_name and self._recent_workspaces is not None:
            for entry in self._recent_workspaces.list_recent():
                ws_raw = str(entry.get("path", ""))
                if ws_raw:
                    roots.append(self._ws_root_of(ws_raw) / self._data_dir_name)
        seen: set[str] = set()
        unique: list[Path] = []
        for root in roots:
            key = str(root)
            if key not in seen:
                seen.add(key)
                unique.append(root)
        return unique

    def _pool_of_agent(self, agent_name: str) -> str:
        """Return the pool an agent belongs to (default main)."""
        return self._pool_for_agent_name(agent_name) or _DEFAULT_AGENT_NAME

    def _pool_for_agent_name(self, agent_name: str) -> str | None:
        """Return the pool for *agent_name*, including dynamic subagent instances.

        The agent→pool map contains main agents and template types.  Dynamic
        subagent instances have names like ``reviewer-abc123``; they inherit
        the pool of their template type.
        """
        if agent_name in self._agent_pool_map:
            return self._agent_pool_map[agent_name]
        for template_type, pool in self._agent_pool_map.items():
            if agent_name.startswith(f"{template_type}-"):
                return pool
        return None

    # ------------------------------------------------------------------
    # Late-binding configuration (called by WebUIService after init)
    # ------------------------------------------------------------------

    def set_pool_agent_names(self, names: list[str]) -> None:
        """Set the list of pool agent names for proactive delta registration."""
        self._pool_agent_names = list(names)

    def set_pool_switch_callback(self, callback: Callable[[str, str], None]) -> None:
        """Set callback for setting pool routing: callback(session_prefix, pool_name)."""
        self._pool_switch_callback = callback

    def set_pool_resolver(self, callback: Callable[[str], str | None]) -> None:
        """Set callback for reading current pool: callback(conv_id) -> pool_name."""
        self._pool_resolver = callback

    def set_agent_resolver(self, callback: Callable[[str], str]) -> None:
        """Set callback for resolving pool_name -> main_agent_name."""
        self._agent_resolver = callback

    def set_data_dir_name(self, data_dir_name: str) -> None:
        """Set the data directory name (e.g. '.modex') for workspace path resolution."""
        self._data_dir_name = data_dir_name

    def set_agent_pool_map(self, mapping: dict[str, str]) -> None:
        """Set mapping from main_agent_name -> pool_name for session list labels."""
        self._agent_pool_map = dict(mapping)

    def set_workspace_control(self, control: WorkspaceControlPort) -> None:
        """Inject the WorkspaceControlPort for the workspace API."""
        self._workspace_control = control

    def set_session_gc(self, gc: SessionGarbageCollector | None) -> None:
        """Inject the SessionGarbageCollector for cascade session deletion."""
        self._session_gc = gc

    def set_store_resolver(
        self,
        resolver: Callable[[Path, str], Awaitable[RuntimeStores]] | None,
    ) -> None:
        """Inject a backend-aware runtime store resolver.

        The resolver returns a :class:`RuntimeStores` for a given workspace
        root + pool. When set, the todos and approvals endpoints use these
        stores instead of the hardcoded file-based stores, so they read from
        the same backend the agent writes to.
        """
        self._store_resolver = resolver

    def set_workspace_index(self, index: WorkspaceIndex) -> None:
        """Inject the session→workspace membership index."""
        self._workspace_index = index

    def set_session_store(self, store: SessionStore) -> None:
        """Inject a single session store for SessionInfo-based operations.

        Used by tests and single-workspace setups. Production multi-live
        wiring should use :meth:`set_session_store_factory` instead so each
        workspace gets a fresh store rooted at its own session-index dir.
        """
        self._session_store = store

    def set_session_store_factory(self, factory: Callable[[Path], Awaitable[SessionStore]]) -> None:
        """Inject a factory that builds a per-workspace session store.

        The factory receives the workspace's session-index directory (resolved
        from the request's ``?ws=``) and returns a :class:`SessionStore`
        rooted there. This replaces the old per-call ``index_dir`` override
        on :class:`SessionStore` methods — workspace isolation now lives in
        store construction, not per-call routing.
        """
        self._session_store_factory = factory

    async def _session_store_for(self, index_dir: Path) -> SessionStore | None:
        """Return a session store scoped to *index_dir*.

        - When a factory is injected (production), build a fresh store rooted
          at *index_dir* — one store per workspace.
        - Otherwise fall back to the single injected store (tests,
          single-workspace) and ignore *index_dir*.
        """
        if self._session_store_factory is not None:
            return await self._session_store_factory(index_dir)
        return self._session_store

    def set_session_factory(self, factory: SessionIdFactory) -> None:
        """Inject the SessionIdFactory for creating new sessions."""
        self._session_factory = factory

    def set_recent_workspaces(self, recent) -> None:
        """Inject the RecentWorkspaces store for the recent-workspaces API."""
        self._recent_workspaces = recent
        recent.load()

    def set_input_pipeline(self, pipeline) -> None:
        """Inject the WebUI user-input pipeline."""
        self._input_pipeline = pipeline

    def set_input_context(self, ctx) -> None:
        """Inject the shared input-pipeline context."""
        self._input_ctx = ctx

    def set_model_config_loader(self, loader: Callable[[], BotModelConfig | None]) -> None:
        """Inject a callable that returns the current BotModelConfig for GET /api/models.

        The loader re-reads config/model.yml so the selector reflects CLI model
        edits without a server restart. Only provider/model names are exposed —
        never api_key/url (handled in _handle_models).
        """
        self._model_config_loader = loader

    def set_config_controller(self, controller: ConfigController) -> None:
        """Inject the ConfigController for /api/config/{domain} and /api/system/restart."""
        self._config_controller = controller

    def set_pool_config_controller(self, controller: PoolConfigController) -> None:
        """Inject the PoolConfigController for /api/pools, /api/mcp, /api/skills."""
        self._pool_config_controller = controller

    # ------------------------------------------------------------------
    # SessionInfo resolution helpers
    # ------------------------------------------------------------------

    async def _resolve_session(self, session_id: str, index_dir: Path | None = None) -> SessionInfo:
        """Resolve a SessionInfo from *session_id*.

        Prefers the session store; falls back to ``SessionInfo.from_str()``
        when no store is injected (e.g. basic tests). *index_dir* scopes the
        lookup to a workspace's session index by constructing a fresh store
        via the injected factory.
        """
        store = (
            await self._session_store_for(index_dir)
            if index_dir is not None
            else self._session_store
        )
        if store is not None:
            session = await store.get(session_id)
            if session is not None:
                return session
        return SessionInfo.from_str(session_id)

    async def _resolve_agent(self, session_id: str, index_dir: Path | None = None) -> str:
        """Return the agent name bound to *session_id*.

        Prefers the authoritative session store; falls back to
        ``SessionInfo.from_str()`` when no store is injected.
        """
        session = await self._resolve_session(session_id, index_dir=index_dir)
        return session.agent_name

    async def _derive_sessions_from_transcripts(
        self, sessions_dir: Path | None = None
    ) -> list[SessionInfo]:
        """Build SessionInfo records from transcript files when the session
        index is missing or incomplete.

        Legacy workspaces only have ``.modex/sessions/<pool>/*.jsonl`` files
        and no ``.modex/session_index/``.  This fallback lets the frontend
        list and attach to those sessions without a separate migration step.
        """
        target_dir = sessions_dir if sessions_dir is not None else self._home_sessions_dir
        derived: list[SessionInfo] = []
        for session_id in await self._store.list_sessions(target_dir):
            session_prefix = session_id_prefix_of(session_id)
            if session_prefix == session_id:
                # No separator → not a usable display id.
                continue
            agent_name = agent_of(session_id)
            # Include any agent that maps to a known pool (main agents,
            # resident subagents, and dynamic subagent template types).
            pool = self._pool_for_agent_name(agent_name)
            if pool is None:
                continue
            parent_session_id: str | None = None
            # Subagent transcript (3 segments): parent is the main-agent
            # session with the same conversation prefix, if one exists.
            if session_id.count(".") == 2:
                candidates = sorted(
                    sid
                    for sid in await self._store.list_sessions_by_prefix(
                        session_prefix, sessions_dir=target_dir
                    )
                    if sid != session_id and sid.count(".") == 1
                )
                if candidates:
                    parent_session_id = candidates[0]
            updated_at = await self._store.last_updated(session_id, sessions_dir=target_dir)
            created_at = updated_at
            derived.append(
                SessionInfo(
                    session_id=session_id,
                    agent_name=agent_name,
                    parent_session_id=parent_session_id,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return derived

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _setup_routes(self) -> None:
        """Register REST and WebSocket routes on the aiohttp Application."""
        # GET /api/pools is registered below (Phase 2B) against
        # _handle_list_pools, which returns the richer PoolSummary shape
        # (a superset of the legacy {name} dict, so fetchPools() still works).
        self.app.router.add_get(_API_MODELS_PATH, self._handle_models)
        self.app.router.add_get("/api/workspace", self._handle_workspace)
        self.app.router.add_get("/api/workspace/browse", self._handle_workspace_browse)
        self.app.router.add_post("/api/workspace/cd", self._handle_workspace_cd)
        self.app.router.add_get("/api/workspace/recent", self._handle_workspace_recent)
        self.app.router.add_get(_API_SESSIONS_PATH, self._handle_sessions)
        self.app.router.add_post(_API_SESSIONS_PATH, self._handle_create_session)
        self.app.router.add_get(f"{_API_SESSIONS_SESSION_PATH}/messages", self._handle_get_messages)
        self.app.router.add_get(f"{_API_SESSIONS_SESSION_PATH}/todos", self._handle_get_todos)
        self.app.router.add_get(
            f"{_API_SESSIONS_SESSION_PATH}/approvals", self._handle_get_approvals
        )
        self.app.router.add_post(
            f"{_API_SESSIONS_SESSION_PATH}/approvals", self._handle_post_approval
        )
        self.app.router.add_get(
            f"{_API_SESSIONS_SESSION_PATH}/attachments/{{attachment_id}}",
            self._handle_download_attachment,
        )
        self.app.router.add_post(
            f"{_API_SESSIONS_SESSION_PATH}/attachments",
            self._handle_upload_attachment,
        )
        self.app.router.add_get(_API_MEDIA_CONFIG_PATH, self._handle_media_config)
        self.app.router.add_delete(_API_SESSIONS_SESSION_PATH, self._handle_delete_session)
        self.app.router.add_get("/api/config/{domain}", self._handle_get_config)
        self.app.router.add_put("/api/config/{domain}", self._handle_put_config)
        self.app.router.add_post("/api/system/restart", self._handle_restart)
        self.app.router.add_post("/api/models/fetch", self._handle_fetch_provider_models)
        # Pool / MCP / skills / prompt REST API (Phase 2B). Mirror the
        # add_<verb> style used above.
        self.app.router.add_get("/api/pools", self._handle_list_pools)
        self.app.router.add_post("/api/pools", self._handle_create_pool)
        self.app.router.add_get("/api/pools/{pool}", self._handle_read_pool)
        self.app.router.add_put("/api/pools/{pool}", self._handle_write_pool)
        self.app.router.add_delete("/api/pools/{pool}", self._handle_delete_pool)
        self.app.router.add_post("/api/pools/{pool}/peers", self._handle_add_peer)
        self.app.router.add_delete("/api/pools/{pool}/peers/{peer}", self._handle_remove_peer)
        self.app.router.add_get("/api/mcp", self._handle_read_mcp)
        self.app.router.add_post("/api/mcp/{server}", self._handle_upsert_mcp)
        self.app.router.add_put("/api/mcp/{server}", self._handle_upsert_mcp)
        self.app.router.add_delete("/api/mcp/{server}", self._handle_delete_mcp)
        self.app.router.add_get("/api/skills", self._handle_list_skills)
        self.app.router.add_post("/api/skills", self._handle_upload_skill)
        self.app.router.add_delete("/api/skills/{name}", self._handle_delete_skill)
        self.app.router.add_get("/api/prompts", self._handle_list_prompts)
        self.app.router.add_post("/api/prompts", self._handle_create_prompt)
        self.app.router.add_get("/api/prompts/{name}", self._handle_read_prompt_strict)
        self.app.router.add_put("/api/prompts/{name}", self._handle_write_prompt_global)
        self.app.router.add_delete("/api/prompts/{name}", self._handle_delete_prompt_global)
        self.app.router.add_get(
            "/api/pools/{pool}/agents/{agent}/skills", self._handle_list_agent_skills
        )
        self.app.router.add_post(
            "/api/pools/{pool}/agents/{agent}/skills/{name}",
            self._handle_assign_skill,
        )
        self.app.router.add_delete(
            "/api/pools/{pool}/agents/{agent}/skills/{name}",
            self._handle_unassign_skill,
        )
        self.app.router.add_get(_WS_PATH, self._handle_websocket)

        if self._static_dist is not None:
            self.app.router.add_get(
                _WEBUI_STATIC_PREFIX,
                self._handle_static_index,
            )
            self.app.router.add_static(
                _WEBUI_STATIC_PREFIX,
                path=str(self._static_dist),
                show_index=False,
            )
        else:
            self.app.router.add_get(_WEBUI_STATIC_PREFIX, self._handle_no_static)
            self.app.router.add_get(f"{_WEBUI_STATIC_PREFIX}{{tail:.*}}", self._handle_no_static)

    # ------------------------------------------------------------------
    # REST handlers
    # ------------------------------------------------------------------

    async def _handle_models(self, request: web.Request) -> web.Response:
        """GET /api/models -- list (provider_name, model_name, default) choices
        for the frontend model selector.

        Re-reads model.yml live so CLI edits appear without a restart. Only
        provider_name / model_name / default are returned — NEVER api_key or url
        (those stay server-side).
        """
        cfg = self._model_config_loader() if self._model_config_loader is not None else None
        if cfg is None:
            return web.json_response({"choices": []})
        default = (cfg.default_provider, cfg.default_model)
        choices = [
            {"provider_name": p, "model_name": m, "default": (p, m) == default}
            for (p, m) in cfg.all_choices()
        ]
        return web.json_response({"choices": choices})

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        """GET /api/config/{domain} -- masked config payload."""
        if self._config_controller is None:
            return web.json_response({"error": "config not configured"}, status=503)
        domain = request.match_info["domain"]
        try:
            payload = self._config_controller.read(domain)
        except KeyError:
            return web.json_response({"error": f"unknown domain: {domain}"}, status=404)
        except Exception as exc:  # noqa: BLE001 - malformed YAML / IO errors surface readably
            logger.exception("config read failed for domain %s", domain)
            return web.json_response({"error": f"config read failed: {exc}"}, status=500)
        return web.json_response(payload.model_dump(mode="json"))

    async def _handle_put_config(self, request: web.Request) -> web.Response:
        """PUT /api/config/{domain} -- validate + persist. Never auto-applies."""
        if self._config_controller is None:
            return web.json_response({"error": "config not configured"}, status=503)
        domain = request.match_info["domain"]
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 - malformed JSON body
            logger.warning("Failed to parse config JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        try:
            payload = self._config_controller.write(domain, body)
        except KeyError:
            return web.json_response({"error": f"unknown domain: {domain}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001 - unexpected write failure
            logger.exception("config write failed for domain %s", domain)
            return web.json_response({"error": "write failed"}, status=500)
        return web.json_response(payload.model_dump(mode="json"))

    async def _handle_restart(self, request: web.Request) -> web.Response:
        """POST /api/system/restart -- schedule a process restart."""
        if self._config_controller is None:
            return web.json_response({"error": "config not configured"}, status=503)
        try:
            self._config_controller.restart()
        except Exception as exc:  # noqa: BLE001 - restart unavailable
            logger.warning("restart failed: %s", exc)
            return web.json_response(
                {
                    "error": "restart unavailable",
                    "hint": "Run `modexbot restart` in your terminal.",
                },
                status=200,
            )
        return web.json_response({"restarting": True})

    # ------------------------------------------------------------------
    # Provider model-list fetch
    # ------------------------------------------------------------------

    async def _close_http_session(self, app: web.Application) -> None:
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
            self._http_session = None

    async def _get_http_session(self) -> ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = ClientSession(timeout=ClientTimeout(total=15))
        return self._http_session

    async def _handle_fetch_provider_models(self, request: web.Request) -> web.Response:
        """POST /api/models/fetch -- fetch a provider's model list server-side.

        Unified schema (``FetchModelsReq``): ``{provider_key?, base_url?,
        api_key?, interface_format?, models_url?}``. Inline fields take
        priority; missing fields fall back to the saved provider looked up
        by ``provider_key`` in ``model.yml``. After merge, ``api_key`` and
        (``base_url`` or ``models_url``) must be non-empty.

        ``api_key`` is never logged; only ``provider_key`` and/or
        ``base_url`` appear in diagnostics.
        """
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001 - malformed JSON body
            logger.warning("fetch_provider_models: bad JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)

        try:
            req = FetchModelsReq.model_validate(body)
        except ValidationError as exc:
            from bot.service.config_controller import _flatten_errors

            return web.json_response(
                {"error": "validation", "fields": _flatten_errors(exc)},
                status=422,
            )

        saved: ProviderCfg | None = None
        if req.provider_key:
            if self._model_config_loader is not None:
                cfg = self._model_config_loader()
                if cfg is not None:
                    saved = cfg.find_provider_by_key(req.provider_key)
            # If saved is None (model.yml missing, unparseable, or key not
            # yet saved), fall through to inline values below instead of
            # erroring — the user may be fetching models for a brand-new
            # provider that hasn't been saved yet.

        base_url = req.base_url or (saved.base_url if saved else "") or ""
        api_key = req.api_key or (saved.api_key if saved else "") or ""
        interface_format = (
            req.interface_format
            or (saved.interface_format if saved else None)
            or InterfaceFormat.OPENAI_COMPATIBLE
        )
        models_url = (
            req.models_url
            if req.models_url is not None
            else (saved.models_url if saved else None)
        )

        if not api_key:
            return web.json_response(
                {"error": "validation", "fields": {"api_key": ["required"]}},
                status=422,
            )
        if not base_url and not models_url:
            return web.json_response(
                {"error": "validation", "fields": {"base_url": ["required"]}},
                status=422,
            )

        log_label = (
            f"provider_key={req.provider_key}"
            if req.provider_key
            else f"base_url={base_url}"
        )
        logger.info("fetch_provider_models: %s", log_label)

        session = await self._get_http_session()
        try:
            models = await fetch_provider_models(
                session=session,
                base_url=base_url,
                api_key=api_key,
                interface_format=interface_format,
                models_url_override=models_url,
            )
        except ModelFetchError as exc:
            return web.json_response({"error": exc.reason, "status": exc.status}, status=502)
        except Exception:  # noqa: BLE001 - unexpected network/parse failure
            logger.exception("fetch_provider_models failed for %s", log_label)
            return web.json_response({"error": "fetch failed"}, status=500)

        return web.json_response({"models": [m.model_dump() for m in models]})

    # ------------------------------------------------------------------
    # Pool / MCP / skills / prompt handlers (Phase 2B)
    # ------------------------------------------------------------------

    def _pool_cfg_required(self) -> web.Response | None:
        """Return a 503 response if no PoolConfigController is wired, else None."""
        if self._pool_config_controller is None:
            return web.json_response({"error": "pool config not configured"}, status=503)
        return None

    async def _handle_list_pools(self, request: web.Request) -> web.Response:
        """GET /api/pools -- list pool summaries."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        try:
            pools = self._pool_config_controller.list_pools()
        except Exception:  # noqa: BLE001
            logger.exception("list_pools failed")
            return web.json_response({"error": "read failed"}, status=500)
        return web.json_response([p.model_dump(mode="json") for p in pools])

    async def _handle_create_pool(self, request: web.Request) -> web.Response:
        """POST /api/pools -- create a pool. Body: {"name": "<pool>"}."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_pool: bad JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return web.json_response(
                {"error": "validation", "fields": {"name": ["required"]}},
                status=400,
            )
        try:
            tree = self._pool_config_controller.create_pool(name)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("create_pool failed")
            return web.json_response({"error": "create failed"}, status=500)
        return web.json_response(tree.model_dump(mode="json"))

    async def _handle_read_pool(self, request: web.Request) -> web.Response:
        """GET /api/pools/{pool} -- read one pool tree."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        pool = request.match_info["pool"]
        try:
            tree = self._pool_config_controller.read_pool(pool)
        except KeyError:
            return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("read_pool failed")
            return web.json_response({"error": "read failed"}, status=500)
        return web.json_response(tree.model_dump(mode="json"))

    async def _handle_write_pool(self, request: web.Request) -> web.Response:
        """PUT /api/pools/{pool} -- validate + persist a pool tree. Body = PoolTree."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        pool = request.match_info["pool"]
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("write_pool: bad JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        try:
            from modex_agent.multi_agent.pool_config import PoolSpec

            tree = PoolSpec.model_validate(body)
        except Exception as exc:  # noqa: BLE001 - pydantic validation
            from bot.service.config_controller import _flatten_errors

            if isinstance(exc, ValidationError):
                return web.json_response(
                    {"error": "validation", "fields": _flatten_errors(exc)},
                    status=400,
                )
            return web.json_response(
                {"error": "validation", "fields": {"body": [str(exc)]}},
                status=400,
            )
        try:
            written = self._pool_config_controller.write_pool(pool, tree)
        except KeyError:
            return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("write_pool failed")
            return web.json_response({"error": "write failed"}, status=500)
        return web.json_response(written.model_dump(mode="json"))

    async def _handle_delete_pool(self, request: web.Request) -> web.Response:
        """DELETE /api/pools/{pool} -- delete a pool."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        pool = request.match_info["pool"]
        try:
            self._pool_config_controller.delete_pool(pool)
        except KeyError:
            return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except PoolNotEmptyError as exc:
            return web.json_response(
                {"error": "pool_not_empty", "busy_agents": exc.busy_agents},
                status=409,
            )
        except Exception:  # noqa: BLE001
            logger.exception("delete_pool failed")
            return web.json_response({"error": "delete failed"}, status=500)
        return web.json_response({"deleted": pool})

    async def _handle_add_peer(self, request: web.Request) -> web.Response:
        """POST /api/pools/{pool}/peers -- add a bidirectional peer edge.

        Body: {"peer": "<other_pool>"}. On success both sides of the edge
        are written and both updated pool trees are returned so the UI can
        refresh the current pool and any visible peer pool.
        """
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        pool = request.match_info["pool"]
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("add_peer: bad JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        peer = body.get("peer") if isinstance(body, dict) else None
        if not isinstance(peer, str) or not peer:
            return web.json_response(
                {"error": "validation", "fields": {"peer": ["required"]}},
                status=400,
            )
        try:
            tree_a, tree_b = self._pool_config_controller.add_peer(pool, peer)
        except KeyError:
            return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("add_peer failed")
            return web.json_response({"error": "add peer failed"}, status=500)
        return web.json_response(
            {
                "pool_a": tree_a.model_dump(mode="json"),
                "pool_b": tree_b.model_dump(mode="json"),
            }
        )

    async def _handle_remove_peer(self, request: web.Request) -> web.Response:
        """DELETE /api/pools/{pool}/peers/{peer} -- remove a bidirectional peer edge.

        Both sides of the edge are removed atomically. Returns both updated
        pool trees.
        """
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        pool = request.match_info["pool"]
        peer = request.match_info["peer"]
        try:
            tree_a, tree_b = self._pool_config_controller.remove_peer(pool, peer)
        except KeyError:
            return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("remove_peer failed")
            return web.json_response({"error": "remove peer failed"}, status=500)
        return web.json_response(
            {
                "pool_a": tree_a.model_dump(mode="json"),
                "pool_b": tree_b.model_dump(mode="json"),
            }
        )

    async def _handle_read_mcp(self, request: web.Request) -> web.Response:
        """GET /api/mcp -- read the typed MCP registry mapping."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        try:
            registry = self._pool_config_controller.read_mcp()
        except Exception:  # noqa: BLE001
            logger.exception("read_mcp failed")
            return web.json_response({"error": "read failed"}, status=500)
        return web.json_response(
            {name: e.model_dump(mode="json", by_alias=True) for name, e in registry.items()}
        )

    async def _handle_upsert_mcp(self, request: web.Request) -> web.Response:
        """POST/PUT /api/mcp/{server} -- insert or update one MCP server entry."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        name = request.match_info["server"]
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("upsert_mcp: bad JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        try:
            entry = self._pool_config_controller.upsert_mcp(name, body)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("upsert_mcp failed")
            return web.json_response({"error": "write failed"}, status=500)
        return web.json_response(entry.model_dump(mode="json", by_alias=True))

    async def _handle_delete_mcp(self, request: web.Request) -> web.Response:
        """DELETE /api/mcp/{server} -- remove one MCP server (refuses if referenced)."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        name = request.match_info["server"]
        try:
            self._pool_config_controller.delete_mcp(name)
        except KeyError:
            return web.json_response({"error": f"unknown server: {name}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("delete_mcp failed")
            return web.json_response({"error": "delete failed"}, status=500)
        return web.json_response({"deleted": name})

    async def _handle_list_skills(self, request: web.Request) -> web.Response:
        """GET /api/skills -- list global skills."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        try:
            skills = self._pool_config_controller.list_skills()
        except Exception:  # noqa: BLE001
            logger.exception("list_skills failed")
            return web.json_response({"error": "read failed"}, status=500)
        return web.json_response([s.model_dump(mode="json") for s in skills])

    async def _handle_list_prompts(self, request: web.Request) -> web.Response:
        """GET /api/prompts -- list agent prompt md files (name/size/mtime)."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        try:
            prompts = self._pool_config_controller.list_prompts()
        except Exception:  # noqa: BLE001
            logger.exception("list_prompts failed")
            return web.json_response({"error": "read failed"}, status=500)
        return web.json_response([p.model_dump(mode="json") for p in prompts])

    async def _handle_read_prompt_strict(self, request: web.Request) -> web.Response:
        """GET /api/prompts/{name} -- read one prompt md WITHOUT seeding.

        Returns 404 when the file is absent (does not call ``read_or_seed``);
        returns 400 on a malformed name.
        """
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        name = request.match_info["name"]
        try:
            prompt = self._pool_config_controller.read_prompt_strict(name)
        except KeyError:
            return web.json_response({"error": f"unknown prompt: {name}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("read_prompt_strict failed")
            return web.json_response({"error": "read failed"}, status=500)
        return web.json_response(prompt.model_dump(mode="json"))

    async def _handle_write_prompt_global(self, request: web.Request) -> web.Response:
        """PUT /api/prompts/{name} -- upsert the prompt md for ``name``.

        Reuses :meth:`PoolConfigController.write_prompt` (atomic write + marks
        ``restart_required`` on the ``prompt`` artifact class). Creates the
        file if absent (upsert semantics — no 409 on existing names).
        """
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        name = request.match_info["name"]
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("write_prompt_global: bad JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        content = body.get("content") if isinstance(body, dict) else None
        if not isinstance(content, str):
            return web.json_response(
                {"error": "validation", "fields": {"content": ["required"]}},
                status=400,
            )
        try:
            prompt = self._pool_config_controller.write_prompt(name, content)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("write_prompt_global failed")
            return web.json_response({"error": "write failed"}, status=500)
        return web.json_response(prompt.model_dump(mode="json"))

    async def _handle_create_prompt(self, request: web.Request) -> web.Response:
        """POST /api/prompts -- create a new prompt md.

        Body: ``{"name": str, "content"?: str}``. Validates the name against the
        agent-name regex; rejects a duplicate name with HTTP 409 via
        :class:`PromptExistsError`. When ``content`` is omitted the seed text
        is :data:`PromptStore.DEFAULT_PROMPT_SEED`.
        """
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_prompt: bad JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        name = body.get("name") if isinstance(body, dict) else None
        if not isinstance(name, str) or not name:
            return web.json_response(
                {"error": "validation", "fields": {"name": ["required"]}},
                status=400,
            )
        content = body.get("content") if isinstance(body, dict) else None
        if content is not None and not isinstance(content, str):
            return web.json_response(
                {"error": "validation", "fields": {"content": ["must be a string"]}},
                status=400,
            )
        try:
            prompt = self._pool_config_controller.create_prompt(name, content)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except PromptExistsError:
            return web.json_response(
                {"error": "exists", "name": name},
                status=409,
            )
        except Exception:  # noqa: BLE001
            logger.exception("create_prompt failed")
            return web.json_response({"error": "create failed"}, status=500)
        return web.json_response(prompt.model_dump(mode="json"), status=201)

    async def _handle_delete_prompt_global(self, request: web.Request) -> web.Response:
        """DELETE /api/prompts/{name} -- delete a prompt md if unreferenced.

        Returns 200 with ``{deleted: str}`` when unreferenced; removes the file.
        Returns 409 with ``{error: "in_use", usages: [...]}`` when any pool's
        main agent or subagent references the prompt (explicit ``prompt_name``
        match or the fallback case where ``prompt_name`` is empty and
        ``agent_name`` equals the prompt name). Returns 404 when the file does
        not exist. Does NOT remove the file on 409.
        """
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        name = request.match_info["name"]
        try:
            self._pool_config_controller.delete_prompt(name)
        except KeyError:
            return web.json_response({"error": f"unknown prompt: {name}"}, status=404)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except PromptInUseError as exc:
            return web.json_response(
                {
                    "error": "in_use",
                    "usages": [u.model_dump(mode="json") for u in exc.usages],
                },
                status=409,
            )
        except Exception:  # noqa: BLE001
            logger.exception("delete_prompt failed")
            return web.json_response({"error": "delete failed"}, status=500)
        return web.json_response({"deleted": name})


    async def _handle_upload_skill(self, request: web.Request) -> web.Response:
        """POST /api/skills -- upload a global skill.

        Accepts multipart/form-data (preferred, matches the frontend
        ``webkitdirectory`` upload): each part's filename is a path under
        ``<skillName>/...``; keys are normalized relative to ``<skillName>/``.
        A text ``name`` form field overrides the skill-name inference from the
        path prefix. Per-file and total-size caps reject oversized uploads.
        """
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        ct = request.content_type or ""
        if ct.startswith("multipart/"):
            return await self._upload_skill_multipart(request)
        return await self._upload_skill_json(request)

    async def _upload_skill_multipart(self, request: web.Request) -> web.Response:
        name: str | None = None
        file_tree: dict[str, bytes] = {}
        total = 0
        try:
            reader = await request.multipart()
        except Exception as exc:  # noqa: BLE001 - not multipart / parser error
            logger.debug("skill upload: multipart unavailable (%s) -- falling back", exc)
            raise _SkillUploadFallback()
        async for part in reader:
            if part.name == "name":
                try:
                    name = (await part.text()).strip()
                except Exception:  # noqa: BLE001
                    name = None
                continue
            filename = part.filename
            if not filename:
                continue
            data = await part.read(decode=False)
            # aiohttp returns bytearray; coerce to bytes for the store.
            if not isinstance(data, (bytes, bytearray)):
                continue
            data = bytes(data)
            if len(data) > _SKILL_MAX_FILE_BYTES:
                return web.json_response(
                    {
                        "error": "validation",
                        "fields": {"file": [f"{filename} exceeds {_SKILL_MAX_FILE_MB}MB"]},
                    },
                    status=400,
                )
            total += len(data)
            if total > _SKILL_MAX_TOTAL_BYTES:
                return web.json_response(
                    {
                        "error": "validation",
                        "fields": {"upload": [f"exceeds {_SKILL_MAX_TOTAL_MB}MB total"]},
                    },
                    status=400,
                )
            rel = _skill_relpath(filename)
            if rel is not None:
                file_tree[rel] = data
        if name is None:
            # Infer the skill name from the first path segment if present.
            for rel in file_tree:
                head = rel.split("/", 1)[0]
                if head and head != rel:
                    name = head
                    break
        if not name:
            return web.json_response(
                {"error": "validation", "fields": {"name": ["required"]}}, status=400
            )
        if not file_tree:
            raise _SkillUploadFallback()
        try:
            entry = self._pool_config_controller.upload_skill(name, file_tree)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("upload_skill failed")
            return web.json_response({"error": "upload failed"}, status=500)
        return web.json_response(entry.model_dump(mode="json"))

    async def _upload_skill_json(self, request: web.Request) -> web.Response:
        """JSON fallback for skill upload: ``{"name": str, "files": {relpath: base64}}``.

        Used when the client cannot submit multipart. Documented deviation;
        the frontend (Task 4.5) is expected to use multipart, but this keeps
        the API usable from environments where multipart is awkward.
        """
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("upload_skill_json: bad JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid body"}, status=400)
        name = body.get("name")
        files = body.get("files")
        if not isinstance(name, str) or not name:
            return web.json_response(
                {"error": "validation", "fields": {"name": ["required"]}}, status=400
            )
        if not isinstance(files, dict) or not files:
            return web.json_response(
                {"error": "validation", "fields": {"files": ["required"]}}, status=400
            )
        import base64

        file_tree: dict[str, bytes] = {}
        total = 0
        for rel_b64, payload in files.items():
            rel = _skill_relpath(rel_b64)
            if rel is None:
                return web.json_response(
                    {"error": "validation", "fields": {"file": [f"unsafe path {rel_b64!r}"]}},
                    status=400,
                )
            if not isinstance(payload, str):
                return web.json_response(
                    {"error": "validation", "fields": {"file": [f"{rel_b64!r} not base64"]}},
                    status=400,
                )
            try:
                data = base64.b64decode(payload)
            except Exception as exc:  # noqa: BLE001
                return web.json_response(
                    {"error": "validation", "fields": {"file": [f"{rel_b64!r} bad base64: {exc}"]}},
                    status=400,
                )
            if len(data) > _SKILL_MAX_FILE_BYTES:
                return web.json_response(
                    {
                        "error": "validation",
                        "fields": {"file": [f"{rel_b64} exceeds {_SKILL_MAX_FILE_MB}MB"]},
                    },
                    status=400,
                )
            total += len(data)
            if total > _SKILL_MAX_TOTAL_BYTES:
                return web.json_response(
                    {
                        "error": "validation",
                        "fields": {"upload": [f"exceeds {_SKILL_MAX_TOTAL_MB}MB total"]},
                    },
                    status=400,
                )
            file_tree[rel] = data
        try:
            entry = self._pool_config_controller.upload_skill(name, file_tree)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("upload_skill_json failed")
            return web.json_response({"error": "upload failed"}, status=500)
        return web.json_response(entry.model_dump(mode="json"))

    async def _handle_delete_skill(self, request: web.Request) -> web.Response:
        """DELETE /api/skills/{name} -- remove a global skill (per-agent copies stay)."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        name = request.match_info["name"]
        try:
            self._pool_config_controller.delete_skill(name)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("delete_skill failed")
            return web.json_response({"error": "delete failed"}, status=500)
        return web.json_response({"deleted": name})

    async def _handle_list_agent_skills(self, request: web.Request) -> web.Response:
        """GET /api/pools/{pool}/agents/{agent}/skills -- list an agent's skills."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        pool = request.match_info["pool"]
        agent = request.match_info["agent"]
        try:
            skills = self._pool_config_controller.list_agent_skills(pool, agent)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("list_agent_skills failed")
            return web.json_response({"error": "read failed"}, status=500)
        return web.json_response([s.model_dump(mode="json") for s in skills])

    async def _handle_assign_skill(self, request: web.Request) -> web.Response:
        """POST /api/pools/{pool}/agents/{agent}/skills/{name} -- assign a skill copy."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        pool = request.match_info["pool"]
        agent = request.match_info["agent"]
        name = request.match_info["name"]
        try:
            self._pool_config_controller.assign_skill(pool, agent, name)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("assign_skill failed")
            return web.json_response({"error": "assign failed"}, status=500)
        return web.json_response({"assigned": name})

    async def _handle_unassign_skill(self, request: web.Request) -> web.Response:
        """DELETE /api/pools/{pool}/agents/{agent}/skills/{name} -- remove a skill copy."""
        if (miss := self._pool_cfg_required()) is not None:
            return miss
        pool = request.match_info["pool"]
        agent = request.match_info["agent"]
        name = request.match_info["name"]
        try:
            self._pool_config_controller.unassign_skill(pool, agent, name)
        except FieldValidationError as exc:
            return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
        except Exception:  # noqa: BLE001
            logger.exception("unassign_skill failed")
            return web.json_response({"error": "unassign failed"}, status=500)
        return web.json_response({"unassigned": name})

    async def _handle_workspace(self, request: web.Request) -> web.Response:
        """GET /api/workspace -- return home path, recent workspaces, and timezone."""
        home = str(self._workspace_control.home) if self._workspace_control is not None else ""
        recent: list[dict[str, object]] = []
        if self._recent_workspaces is not None:
            recent = [
                {"path": r.get("path")}
                for r in self._recent_workspaces.list_recent()
                if isinstance(r, dict) and "path" in r
            ]
        return web.json_response(
            {"home": home, "recent": recent, "timezone": str(get_user_timezone())}
        )

    async def _handle_workspace_browse(self, request: web.Request) -> web.Response:
        """GET /api/workspace/browse?path=<dir> -- list directory contents."""
        raw = request.query.get("path", "")
        target = Path(raw).expanduser() if raw else Path.home()

        if not target.is_absolute():
            target = resolve_workspace_root() / target
        target = target.resolve(strict=False)
        if not target.is_dir():
            target = Path.home()

        # The directory walk is pure synchronous I/O — run it off the event
        # loop so one slow/large directory cannot block other requests.
        def _walk(directory: Path) -> tuple[list[dict[str, object]], str, list[dict[str, object]]]:
            entries: list[dict[str, object]] = []
            try:
                for child in sorted(directory.iterdir()):
                    try:
                        is_dir = child.is_dir()
                    except OSError:
                        continue
                    if not is_dir and not child.is_file():
                        continue
                    entries.append(
                        {
                            "name": child.name,
                            "path": str(child),
                            "is_dir": is_dir,
                        }
                    )
            except PermissionError:
                pass
            entries.sort(key=lambda e: (not bool(e["is_dir"]), str(e["name"]).lower()))
            parent_path = str(directory.parent) if directory.parent != directory else ""

            drives: list[dict[str, object]] = []
            if directory == directory.parent:
                import platform
                import string

                if platform.system() == "Windows":
                    from pathlib import Path as _P

                    for letter in string.ascii_uppercase:
                        drive = _P(f"{letter}:\\")
                        if drive.exists():
                            drives.append(
                                {
                                    "name": f"{letter}:",
                                    "path": str(drive),
                                    "is_dir": True,
                                }
                            )
            return entries, parent_path, drives

        entries, parent_path, drives = await asyncio.to_thread(_walk, target)

        return web.json_response(
            {
                "path": str(target),
                "parent": parent_path,
                "entries": entries,
                "drives": drives,
            }
        )

    async def _handle_workspace_cd(self, request: web.Request) -> web.Response:
        """POST /api/workspace/cd -- change current workspace directory."""
        if self._workspace_control is None:
            return web.json_response(
                {"success": False, "cwd": "", "notice": "Workspace not configured"},
                status=503,
            )
        target: str = ""
        try:
            body = await request.json()
        except Exception as exc:
            logger.warning("Failed to parse workspace/cd JSON body: %s", exc)
            return web.json_response({"error": "invalid body"}, status=400)
        if isinstance(body, dict):
            raw = body.get("path", "")
            if isinstance(raw, str):
                target = raw.strip()
        if not target:
            target = str(self._workspace_control.home)
        result = await self._workspace_control.open_workspace(
            target
        )  # registers the workspace without mutating the agent_pool_map
        if result.success and self._recent_workspaces is not None:
            self._recent_workspaces.add(str(result.current_path))
        return web.json_response(
            {
                "success": result.success,
                "cwd": str(result.current_path),
                "notice": result.notice,
            }
        )

    async def _handle_workspace_recent(self, request: web.Request) -> web.Response:
        """GET /api/workspace/recent -- return recently visited workspace paths."""
        if self._recent_workspaces is None:
            return web.json_response({"recent": []})
        return web.json_response(
            {
                "recent": self._recent_workspaces.list_recent(),
            }
        )

    async def _handle_create_session(self, request: web.Request) -> web.Response:
        """POST /api/sessions -- create a new session.

        Optional JSON body: ``{"pool": "pool_name", "ws": "<workspace path>"}``.
        ``ws`` scopes the new session to a workspace's session index (home when
        absent) so it never leaks into another workspace's listing.
        """
        pool_name: str | None = None
        ws_raw: str = ""
        try:
            body = await request.json()
            if isinstance(body, dict):
                raw_pool = body.get("pool")
                if isinstance(raw_pool, str) and raw_pool:
                    pool_name = raw_pool
                raw_ws = body.get("ws")
                if isinstance(raw_ws, str):
                    ws_raw = raw_ws
        except Exception as exc:
            logger.warning("Failed to parse /api/sessions JSON body: %s", exc)
        index_dir = self._index_dir_of_ws(ws_raw)

        effective_pool = pool_name or _DEFAULT_AGENT_NAME
        agent_name = (
            self._agent_resolver(effective_pool)
            if self._agent_resolver is not None
            else effective_pool
        )
        if self._session_factory is not None:
            session = self._session_factory.create(agent_name)
            session_id = session.session_id
            session_prefix = session.session_id_prefix
            created_at = session.created_at
            updated_at = session.updated_at
            store = await self._session_store_for(index_dir)
            if store is not None:
                await store.save(session)
        else:
            uuid_prefix = _new_uuid_prefix()
            session_id = f"{uuid_prefix}.{agent_name}"
            session_prefix = uuid_prefix
            created_at = None
            updated_at = None
        set_conv_channel(session_prefix, "websocket")
        if self._pool_switch_callback is not None:
            await asyncio.to_thread(self._pool_switch_callback, session_prefix, effective_pool)
        return web.json_response(
            {
                "session_id": session_id,
                "agent_name": agent_name,
                "pool": effective_pool,
                "parent_session_id": None,
                "created_at": created_at,
                "updated_at": updated_at,
            }
        )

    async def _handle_sessions(self, request: web.Request) -> web.Response:
        """GET /api/sessions -- list sessions visible in the current workspace.

        Query ``?pool=X`` to filter to a single pool (default: all pools).
        Query ``?ws=<path>`` to filter to a specific workspace directory.
        All sessions are listed; the frontend builds the tree from
        ``parent_session_id`` — root nodes have ``parent_session_id: null``.

        Sessions are hard-partitioned by workspace: the listing reads ONLY this
        workspace's session index + transcript dir. Home (no ``?ws=``) lists
        only home's sessions — it never leaks other workspaces' sessions.

        Falls back to deriving SessionInfo records from transcript files when
        the session index is empty or incomplete, so legacy workspaces (which
        only have ``.modex/sessions/``) still render existing conversations.
        """
        pool_filter: str | None = request.query.get("pool")
        ws_raw = request.query.get("ws", "")
        index_dir = self._index_dir_of_ws(ws_raw)
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        session_list: list[SessionListEntry] = []
        seen_session_ids: set[str] = set()

        store = await self._session_store_for(index_dir)
        if store is not None:
            for session in await store.list_sessions():
                session_id = session.session_id
                # The store reads recursively, so a record may exist in both a
                # legacy flat layout and a pool subdirectory.  De-dup by id so
                # each conversation appears exactly once.
                if session_id in seen_session_ids:
                    continue
                agent_name = session.agent_name
                # Show sessions for any agent that maps to a known pool
                # (main agents, resident subagents, and dynamic subagent types).
                pool = self._pool_for_agent_name(agent_name)
                if pool is None:
                    continue
                if pool_filter and pool != pool_filter:
                    continue
                seen_session_ids.add(session_id)
                session_list.append(_entry_from_session(session, pool))

        # Fallback: derive any sessions that have transcripts but are not yet
        # indexed.  This covers legacy data created before the SessionInfo index
        # existed and lets the user interact with them immediately.
        for session in await self._derive_sessions_from_transcripts(sessions_dir):
            session_id = session.session_id
            if session_id in seen_session_ids:
                continue
            agent_name = session.agent_name
            pool = self._pool_for_agent_name(agent_name)
            if pool is None:
                continue
            if pool_filter and pool != pool_filter:
                continue
            seen_session_ids.add(session_id)
            session_list.append(_entry_from_session(session, pool))

        session_list.sort(key=lambda s: s.updated_at or 0, reverse=True)
        return web.json_response([asdict(entry) for entry in session_list])

    async def _handle_get_messages(self, request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/messages -- load transcript events.

        Returns user messages (as-is) and materialized assistant turns
        (synthetic assistant_turn dicts with blocks), merged by timestamp.
        """
        session_id: str = request.match_info["session_id"]
        # HTTP handlers run outside any dispatch turn, so the ctxvar is not
        # bound — resolve the sessions dir explicitly from ?ws=.
        ws_raw = request.query.get("ws", "")
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        index_dir = self._index_dir_of_ws(ws_raw)
        agent_name: str = await self._resolve_agent(session_id, index_dir=index_dir)
        pool: str = self._pool_of_agent(agent_name)
        session_prefix: str = session_id_prefix_of(session_id)

        store = self._store

        user_events: list[dict[str, object]] = [
            e.to_dict()
            for e in await store.load_sessions_by_prefix(
                session_prefix, sessions_dir=sessions_dir, pool=pool
            )
            if e.event == "user_message"
        ]

        turns = await store.load_materialized_by_prefix(
            session_prefix, sessions_dir=sessions_dir, pool=pool
        )
        assistant_events: list[dict[str, object]] = []
        for t in turns:
            assistant_events.append(
                {
                    "event": "assistant_turn",
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "timestamp": t.started_at,
                    "turn_id": t.turn_id,
                    "blocks": t.blocks,
                    "latency_ms": 0,
                    # G7: SendFileToUserTool persists outbound Attachment records on
                    # an AssistantTurnEvent; _materialize_events collects them onto
                    # MaterializedTurn.attachments (including the standalone
                    # no-turn_id carriers G7 writes) so they survive a refresh.
                    "attachments": t.attachments,
                }
            )

        result = user_events + assistant_events

        # Partial streaming events — in-memory buffer, queried separately
        # from the main transcript. Attached as a synthetic streaming turn.
        load_partial = getattr(self._store, "load_partial", None)
        if load_partial is not None:
            partial_events = await load_partial(session_id, sessions_dir=sessions_dir)
            if partial_events:
                partial_turn = _materialize_partial_deltas(partial_events, agent_name)
                if partial_turn is not None:
                    result.append(partial_turn)

        def _event_ts(event: dict[str, object]) -> int:
            ts = event.get("timestamp", 0)
            if ts is None:
                return 0
            try:
                return int(str(ts))
            except (ValueError, TypeError):
                return 0

        result.sort(key=_event_ts)
        return web.json_response(result)

    async def _handle_get_todos(self, request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/todos -- load active todos.

        Reads directly from the per-session TodoStore so the frontend can
        hydrate the todo panel when a session is reopened, even before any
        live ``todo_write``/``todo_read`` tool call arrives.

        Uses the backend-aware store from ``_store_resolver`` when wired
        (SQLite mode), falling back to ``JsonFileTodoStore`` for FILE mode.
        """
        session_id: str = request.match_info["session_id"]
        ws_raw = request.query.get("ws", "")
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        index_dir = self._index_dir_of_ws(ws_raw)
        agent_name: str = await self._resolve_agent(session_id, index_dir=index_dir)
        pool: str = self._pool_of_agent(agent_name)

        store = None
        if self._store_resolver is not None:
            stores = await self._store_resolver(self._ws_root_of(ws_raw), pool)
            store = stores.todo_store
        if store is None:
            todo_dir = WorkspacePaths(root=sessions_dir.parent).runtime_dir(pool, "todos")
            store = JsonFileTodoStore(todo_dir)
        items = await store.get(session_id)
        active = [
            {"content": item.content, "status": item.status.value}
            for item in items
            if item.status in (TodoStatus.PENDING, TodoStatus.IN_PROGRESS)
        ]
        return web.json_response(active)

    async def _handle_get_approvals(self, request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/approvals -- pending approvals (webui-only).

        Reads the persisted turn snapshots directly from the pool's turn store
        (same direct-read pattern as :meth:`_handle_get_todos`), so this
        works for restart/refresh recovery without a live pipeline reference.

        Uses the backend-aware store from ``_store_resolver`` when wired
        (SQLite mode), falling back to ``JsonFileTurnStateStore`` for FILE mode.
        """
        from modex_agent.agents.react.state import (
            ReActRuntimeStateCodec,
            ReActSnapshotPolicy,
        )
        from modex_agent.approval.constants import ApprovalDecision
        from modex_agent.approval.views import view_from_request
        from modex_agent.runtime.codec import RuntimeStateCodecRegistry
        from modex_agent.runtime.enums import (
            AgentKind,
            SnapshotReason,
            TurnPhase,
        )
        from modex_agent.runtime.models import StateQueryScope
        from modex_agent.runtime.store import JsonFileTurnStateStore

        session_id: str = request.match_info["session_id"]
        ws_raw = request.query.get("ws", "")
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        agent_name: str = await self._resolve_agent(
            session_id, index_dir=self._index_dir_of_ws(ws_raw)
        )
        pool: str = self._pool_of_agent(agent_name)

        turn_store = None
        if self._store_resolver is not None:
            stores = await self._store_resolver(self._ws_root_of(ws_raw), pool)
            turn_store = stores.turn_store
        if turn_store is None:
            turns_dir = WorkspacePaths(root=sessions_dir.parent).runtime_dir(pool, "turns")
            codec_registry = RuntimeStateCodecRegistry({AgentKind.REACT: ReActRuntimeStateCodec()})
            turn_store = JsonFileTurnStateStore(turns_dir, codec_registry)
        # Approval turns are partitioned by workspace (turn_store path) + pool
        # + session_id, so agent_id is NOT a query dimension — matches
        # ApprovalResumer.load_pending. session_id already identifies the
        # conversation uniquely.
        snapshots = await turn_store.list_active_turns(
            StateQueryScope(
                session_id=session_id,
                phase=TurnPhase.SUSPENDED,
                reason=SnapshotReason.TOOL_APPROVAL_REQUIRED,
            )
        )
        if not snapshots:
            return web.json_response([])
        snapshots.sort(key=lambda s: s.created_at)
        approval = ReActSnapshotPolicy.approval_from_snapshot(snapshots[-1])
        # Surface only genuinely-PENDING requests: already-decided cards must
        # not reappear after a refresh, which would force the user to re-approve.
        views = [
            view_from_request(req).to_dict()
            for req in (approval.requests if approval is not None else [])
            if approval.decisions.get(req.tool_call_id, ApprovalDecision.PENDING)
            == ApprovalDecision.PENDING
        ]
        return web.json_response(views)

    async def _handle_post_approval(self, request: web.Request) -> web.Response:
        """POST /api/sessions/{session_id}/approvals -- submit approve/deny (webui).

        Builds an envelope carrying the structured decision and runs it through
        the webui input pipeline (reusing workspace/pool/session resolution),
        converging on the agent pipeline's approval branch.
        """
        from bot.input_pipeline.stages.resolve_pool import RoutingMeta
        from modex_agent.approval.types import ApprovalAction
        from modex_agent.approval.views import ApprovalDecisionInput
        from modex_agent.input_pipeline.envelope import UserInputEnvelope

        session_id: str = request.match_info["session_id"]
        try:
            payload = await request.json()
            action = ApprovalAction(payload["action"])
        except (KeyError, ValueError, json.JSONDecodeError):
            return web.json_response({"error": "invalid action"}, status=400)
        try:
            tool_call_id = payload["tool_call_id"]
        except KeyError:
            return web.json_response({"error": "missing tool_call_id"}, status=400)

        decision = ApprovalDecisionInput(tool_call_id=tool_call_id, action=action)
        ws_raw = request.query.get("ws", "")
        session = await self._resolve_session(session_id, index_dir=self._index_dir_of_ws(ws_raw))
        envelope = UserInputEnvelope(
            external_id=session_id,
            content="",
            channel="websocket",
            metadata={RoutingMeta.APPROVAL_DECISION: decision},
            pre_resolved_session=session,
        )
        # Stamp the workspace (same resolver as _ws_send_message) so resume
        # reads the turn store that holds this snapshot — without it, the
        # decision silently lands on the home workspace.
        envelope.metadata[RoutingMeta.WORKSPACE] = str(self._ws_root_of(ws_raw))
        # _input_pipeline / _input_ctx are injected by WebUIService. They may
        # be None in minimal test setups -- guard so the handler degrades cleanly.
        if self._input_pipeline is None or self._input_ctx is None:
            return web.json_response({"error": "input pipeline not configured"}, status=503)
        await self._input_pipeline.handle(envelope, self._input_ctx)
        return web.json_response({"accepted": True}, status=202)

    async def _handle_download_attachment(self, request: web.Request) -> web.Response:
        """GET /api/sessions/{session_id}/attachments/{attachment_id}?ws=<ws>.

        Attachment download — one endpoint, dispatch on the record's
        ``locator`` (ADR-0013 §4/§5):

        - ``media`` (inbound): resolve the byte file through the business
          :class:`WorkspaceScopedMediaStore` against the ``?ws=``-resolved media
          dir and the session's pool.
        - ``workspace`` (outbound): the file is at the literal absolute path the
          agent wrote (``att.path``).

        The ``attachment_id`` is an unguessable uuid and IS the capability — no
        auth, no signing (the WebUI is unauthenticated; ADR-0013 §5). ``?ws=``
        is routing only, resolved through the same ``_ws_root_of`` every other
        endpoint uses.

        Streaming + ``Range``/``206`` come from the HTTP layer
        (:class:`aiohttp.web.FileResponse`), not hand-rolled — outbound files
        may be up to 1 GB and must never buffer whole into memory. MIME is
        allow-listed: only ``image/*`` and ``video/*`` keep their real
        ``Content-Type``; everything else is ``application/octet-stream`` so a
        browser cannot sniff executable content. SVG responses carry a strict
        CSP. A present record whose underlying file is gone (evicted inbound,
        deleted outbound) degrades symmetrically to 404 (ADR-0013 §3/§5).
        """
        from bot.service.attachment_index import find_attachment
        from modex_agent.media.models import AttachmentLocator

        session_id: str = request.match_info["session_id"]
        attachment_id: str = request.match_info["attachment_id"]
        ws_raw = request.query.get("ws", "")
        sessions_dir = self._sessions_dir_of_ws(ws_raw)

        att = await find_attachment(
            self._store, session_id, attachment_id, sessions_dir=sessions_dir
        )
        if att is None:
            return web.Response(status=404, text="attachment not found")

        path: Path | None
        if att.locator is AttachmentLocator.MEDIA:
            # Inbound: bytes are under the managed media dir. Resolve the pool
            # for the media resolver the same way the other read handlers
            # resolve it (agent_name -> pool), then read through the business
            # WorkspaceScopedMediaStore with an explicit media_dir (HTTP readers
            # run outside any dispatch turn, so the ctxvar root is unbound).
            index_dir = self._index_dir_of_ws(ws_raw)
            agent_name = await self._resolve_agent(session_id, index_dir=index_dir)
            pool = self._pool_of_agent(agent_name)
            media_store = self._input_ctx.media_store if self._input_ctx is not None else None
            if media_store is None:
                # No media resolver wired — cannot serve inbound bytes.
                return web.Response(status=404, text="attachment not found")
            media_dir = self._media_dir_of_ws(ws_raw, pool)
            path = media_store.store_for(pool, media_dir=media_dir).read(session_id, attachment_id)
        elif att.locator is AttachmentLocator.WORKSPACE:
            # Outbound: the file is at the literal absolute path the agent gave.
            path = Path(att.path)
            if not path.is_absolute():
                logger.warning(
                    "Outbound attachment %s path is not absolute: %s",
                    attachment_id,
                    att.path,
                )
                return web.Response(status=404, text="attachment not found")
        else:  # Defensive — unknown locator value.
            logger.warning("Unknown attachment locator %r for %s", att.locator, attachment_id)
            return web.Response(status=404, text="attachment not found")

        # Symmetric 404: the Attachment record exists in the transcript, but the
        # underlying file is gone (evicted inbound / deleted outbound).
        if path is None or not path.is_file():
            return web.Response(status=404, text="attachment not found")

        # MIME allow-list: only image/* and video/* keep their real Content-Type.
        mime = att.mime or "application/octet-stream"
        serve_mime = (
            mime
            if (mime.startswith("image/") or mime.startswith("video/"))
            else "application/octet-stream"
        )

        headers: dict[str, str] = {
            "Content-Type": serve_mime,
            # nosniff: stop IE/Edge from MIME-sniffing an octet-stream body into
            # executable content (defense in depth on top of the MIME allow-list).
            "X-Content-Type-Options": "nosniff",
        }
        # SVG can carry inline script/style — pin a strict CSP so a downloaded
        # SVG opened in a browser tab cannot execute or exfiltrate.
        if serve_mime == "image/svg+xml":
            headers["Content-Security-Policy"] = (
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
            )
        # FileResponse streams the file (chunk_size) and handles HTTP Range /
        # 206 Partial Content natively, so up-to-1 GB outbound never buffers.
        return web.FileResponse(path, headers=headers)

    async def _handle_media_config(self, request: web.Request) -> web.Response:
        """GET /api/media/config -- expose MediaConfig limits for pre-validation.

        Returns the active ``MediaConfig`` numbers the frontend needs to
        pre-validate a selection before uploading (ADR-0013 §7). v1 is a single
        shared config (per-pool override is a later extension; the ingest stage
        reads the same instance off the input context). When no input context is
        wired (minimal tests), the frozen ``MediaConfig()`` defaults are
        returned so the endpoint always answers with the authoritative numbers.
        """
        from modex_agent.multi_agent.pool_config.media import MediaConfig

        config: MediaConfig = (
            self._input_ctx.media_config if self._input_ctx is not None else MediaConfig()
        )
        return web.json_response(
            {
                "max_image_bytes": config.max_image_bytes,
                "max_text_doc_bytes": config.max_text_doc_bytes,
                "session_budget_bytes": config.session_budget_bytes,
                "max_outbound_bytes": config.max_outbound_bytes,
            }
        )

    async def _handle_upload_attachment(self, request: web.Request) -> web.Response:
        """POST /api/sessions/{session_id}/attachments -- temp-file receiver.

        This endpoint is a **temp-file receiver + pre-stash**, NOT the
        authority. It saves the uploaded file under the workspace's media
        ``_tmp`` dir and returns a ref the frontend includes in the subsequent
        WS user message as an ``AttachmentRef(local_path=...)``. The actual
        perception gate + ``MediaStore.save`` + Attachment record happen in the
        ingest stage (G3) when the WS message flows through the pipeline — the
        gate stays the single authority (no duplicate gate logic here).

        A loose size pre-check rejects absurd uploads early (cap is the larger
        of the image/text-doc limits, generous on purpose); the authoritative
        per-kind cap is the pipeline's. ``?ws=`` resolves the workspace the same
        way every other handler does.
        """
        from modex_agent.multi_agent.pool_config.media import MediaConfig

        session_id: str = request.match_info["session_id"]
        ws_raw = request.query.get("ws", "")

        index_dir = self._index_dir_of_ws(ws_raw)
        agent_name = await self._resolve_agent(session_id, index_dir=index_dir)
        pool = self._pool_of_agent(agent_name)

        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file":
            return web.json_response({"error": "missing 'file' part"}, status=400)

        config = (
            self._input_ctx.media_config_for(pool) if self._input_ctx is not None else MediaConfig()
        )
        # Loose early cap: reject anything above the most generous accepted
        # limit. The authoritative per-kind gate runs in the ingest stage.
        early_cap = max(config.max_image_bytes, config.max_text_doc_bytes)

        tmp_dir = self._media_tmp_dir_of_ws(ws_raw, pool)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_name = uuid4().hex
        tmp_path = tmp_dir / tmp_name

        size = 0
        try:
            with tmp_path.open("wb") as out:
                while True:
                    chunk = await part.read_chunk(_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > early_cap:
                        out.close()
                        tmp_path.unlink(missing_ok=True)
                        return web.json_response({"error": "file too large"}, status=413)
                    out.write(chunk)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("could not remove temp upload %s", tmp_path)
            raise

        return web.json_response(
            {
                "local_path": str(tmp_path),
                "filename": part.filename or tmp_name,
                "size": size,
                "mime": part.headers.get("Content-Type"),
            }
        )

    async def _handle_delete_session(self, request: web.Request) -> web.Response:
        """DELETE /api/sessions/{session_id} -- delete a conversation's cascade.

        Delegates to the SessionGarbageCollector: resolves the workspace root +
        pool, then the collector sync-removes the root's record + transcript
        (conversation leaves the list immediately) and drains the full subagent
        cascade + all ten artifact types via the background pool. Keeps the
        {deleted: id} contract. If no collector is wired (should not happen in
        production), logs a warning and returns without deleting.
        """
        session_id: str = request.match_info["session_id"]
        if self._session_gc is None:
            # No collector wired (should not happen in production — web_ui_service
            # wires one unconditionally at start). Surface it loudly rather than
            # silently re-running the old shallow delete (which would skip the
            # cascade + artifacts this feature exists to clean).
            logger.warning(
                "delete_session: no SessionGarbageCollector wired; skipping cascade "
                "deletion for %s",
                session_id,
            )
            return web.json_response({"deleted": session_id})
        ws_raw = request.query.get("ws", "")
        ws_root = self._ws_root_of(ws_raw)
        index_dir = self._index_dir_of_ws(ws_raw)
        sessions_dir = self._sessions_dir_of_ws(ws_raw)
        resolved = await self._resolve_session(session_id, index_dir=index_dir)
        pool = self._pool_of_agent(resolved.agent_name)
        await self._session_gc.delete_session_tree(session_id, ws_root=ws_root, pool=pool)
        clear_partial = getattr(self._store, "clear_partial", None)
        if clear_partial is not None:
            try:
                await clear_partial(session_id, sessions_dir=sessions_dir)
            except Exception as exc:
                logger.warning("clear_partial failed during delete for %s: %s", session_id, exc)
        return web.json_response({"deleted": session_id})

    # ------------------------------------------------------------------
    # Static fallback
    # ------------------------------------------------------------------

    async def _handle_static_index(self, request: web.Request) -> web.FileResponse:
        """Serve index.html from the static dist directory."""
        assert self._static_dist is not None
        return web.FileResponse(self._static_dist / "index.html")

    async def _handle_no_static(self, request: web.Request) -> web.Response:
        """Return 503 when static files are not configured."""
        return web.Response(status=503, text="WebUI static files not configured")

    # ------------------------------------------------------------------
    # WebSocket handler
    # ------------------------------------------------------------------

    async def _handle_websocket(self, request: web.Request) -> web.WebSocketResponse:
        """WebSocket endpoint -- handles attach, send_message, new/delete session."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        state = _WsConnectionState()

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await self._dispatch_ws_message(ws, msg.data, state)
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", ws.exception())
        except Exception:
            logger.exception("WebSocket handler error")
        finally:
            await state.cleanup(self._input)

        return ws

    async def _dispatch_ws_message(
        self,
        ws: web.WebSocketResponse,
        raw: str,
        state: _WsConnectionState,
    ) -> None:
        """Parse and dispatch a single WebSocket text message."""
        try:
            data: dict[str, object] = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON in WebSocket message")
            return

        action = str(data.get("action", ""))

        if action == WebSocketAction.ATTACH:
            await self._ws_attach(ws, data, state)
        elif action == WebSocketAction.SEND_MESSAGE:
            await self._ws_send_message(ws, data, state)
        elif action == WebSocketAction.PAUSE:
            await self._ws_pause(ws, data)
        elif action == WebSocketAction.DELETE_CONVERSATION:
            await self._ws_delete_conversation(ws, data)
        else:
            logger.warning("Unknown WebSocket action: %s", action)

    # -- action handlers -----------------------------------------------------

    async def _ws_pause(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
    ) -> None:
        """Cancel the running turn for the selected session.

        The WebSocket input adapter is configured with the shared control filter,
        so reusing _try_intercept_control("/stop", ...) sends a CANCEL_TURN
        command through InMemoryControlChannel. The interceptors in the active
        pool drain the command and abort the turn.

        When the control command is not handled (filter not configured, or an
        unexpected parse failure), an error envelope is surfaced to the client
        so the pause button never silently does nothing.
        """
        session_id = str(data.get("session_id", ""))
        if "." not in session_id:
            return

        ws_raw = str(data.get("ws", ""))
        index_dir = self._index_dir_of_ws(ws_raw)
        resolved = await self._resolve_session(session_id, index_dir=index_dir)
        handled = await self._input._try_intercept_control("/stop", resolved.session_id)
        if not handled:
            pool = self._pool_of_agent(resolved.agent_name)
            await _safe_send_json(
                ws,
                DeltaEnvelope(
                    session_id=resolved.session_id,
                    agent_name=resolved.agent_name,
                    event_type=WebUIEventType.ERROR.value,
                    pool=pool,
                    payload={"message": "No turn to pause — the agent is currently idle."},
                ).to_dict(),
            )

    async def _ws_attach(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
        state: _WsConnectionState,
    ) -> None:
        session_id = str(data.get("session_id", ""))

        # The workspace ("ws") the client attached under — scopes every
        # transcript / session-index read in this attach so history and
        # subagent discovery never cross workspace boundaries. Empty == home.
        attach_ws_raw = str(data.get("ws", ""))
        attach_sessions_dir = self._sessions_dir_of_ws(attach_ws_raw)
        attach_index_dir = self._index_dir_of_ws(attach_ws_raw)

        # ── New conversation path: frontend sends uuid_prefix + pool ──
        uuid_prefix_raw = str(data.get("uuid_prefix", ""))
        pool_from_client = str(data.get("pool", ""))

        if uuid_prefix_raw and pool_from_client:
            agent_name = (
                self._agent_resolver(pool_from_client)
                if self._agent_resolver is not None
                else pool_from_client
            )
            if self._pool_agent_names and agent_name not in self._pool_agent_names:
                await _safe_send_json(
                    ws,
                    DeltaEnvelope(
                        session_id=session_id or "",
                        agent_name=agent_name,
                        event_type=WebUIEventType.ERROR.value,
                        pool=pool_from_client,
                        payload={"message": f"unknown pool: {pool_from_client}"},
                    ).to_dict(),
                )
                return
            # Deferred creation: empty drafts are NOT persisted — the client's
            # uuid_prefix is used verbatim as the session_prefix so the session id
            # (``{uuid_prefix}.{agent}``) stays stable through attach→send.
            # Persistence happens on the first message (_ws_send_message).
            session_id = f"{uuid_prefix_raw}.{agent_name}"
            session_prefix = uuid_prefix_raw
            uuid_prefix = uuid_prefix_raw
            explicit_agent = agent_name

            # Defensive: if a transcript already exists for this session_id
            # (reattach of a persisted session that already received a message),
            # routing is already established — attach is idempotent.
            try:
                if await self._store.load(session_id, sessions_dir=attach_sessions_dir):
                    pass  # Session persisted; attach is idempotent, routing intact.
            except Exception as exc:
                logger.warning("Failed to check existing transcript for %s: %s", session_id, exc)
        else:
            if not session_id or "." not in session_id:
                await _safe_send_json(
                    ws,
                    DeltaEnvelope(
                        session_id=session_id or "",
                        agent_name=_DEFAULT_AGENT_NAME,
                        event_type=WebUIEventType.ERROR.value,
                        payload={"message": "session_id required"},
                    ).to_dict(),
                )
                return
            resolved = await self._resolve_session(session_id, index_dir=attach_index_dir)
            session_prefix = resolved.session_id_prefix
            uuid_prefix = session_prefix
            explicit_agent = resolved.agent_name

        # Unregister any previous sessions and cancel their forward tasks.
        # cleanup() sets state._stopped (to halt the previous watcher); reset
        # it here because this state is being reused for a fresh attach cycle
        # and the new watcher spawned below must run.
        await state.cleanup(self._input)
        state._stopped = False

        self._input.register_connection(session_id, ws)
        state.attached_sessions.append(session_id)

        # PoolRouter's session store is the single source of truth for routing.
        # pool_from_client is the user's explicit choice from the UI dropdown;
        # use it directly as the pool name without going through agent_pool_map
        # (which may not yet be populated in every edge case).
        pool_name = pool_from_client if pool_from_client else None
        if not pool_name and explicit_agent and self._agent_pool_map:
            pool_name = self._agent_pool_map.get(explicit_agent)
        if not pool_name and self._pool_resolver is not None:
            pool_name = self._pool_resolver(uuid_prefix)
        if not pool_name:
            pool_name = _DEFAULT_AGENT_NAME
        if self._pool_switch_callback is not None:
            await asyncio.to_thread(self._pool_switch_callback, session_prefix, pool_name)
        # Failsafe: if the callback is not wired (edge case during early
        # startup or test setups), write directly through the input context's
        # pool_session_store so the PoolRouter can still read the mapping.
        elif self._input_ctx is not None and self._input_ctx.pool_session_store is not None:
            self._input_ctx.pool_session_store.set(session_prefix, pool_name)

        # Proactively register ALL pool agent sessions so deltas from any
        # pool's agent are forwarded to this WebSocket client.
        # Use the already-resolved session_prefix (encoded for new conversations,
        # the persisted session_prefix for existing sessions) so the derived ids
        # match the transcript/delta-queue keys — do NOT re-encode.
        for agent_name in self._pool_agent_names:
            if agent_name == _DEFAULT_AGENT_NAME:
                continue  # already registered above
            pool_sid = f"{session_prefix}.{agent_name}"
            if self._input.get_delta_queue(pool_sid) is None:
                self._input.register_connection(pool_sid, ws)
                state.attached_sessions.append(pool_sid)
                state.forward_tasks.append(asyncio.create_task(self._forward_deltas(pool_sid, ws)))

        # Also register subagent sessions found in transcript (for history).
        # These are full session ids (``{conv}.{agent}.{invocation_id}``); each
        # invocation is a distinct session.  ``session_prefix`` is the stable
        # conversation prefix used by the transcript store.
        for sub_sid in sorted(
            await self._store.list_sessions_by_prefix(
                session_prefix, sessions_dir=attach_sessions_dir
            )
        ):
            sub_agent_name = agent_of(sub_sid, default="unknown")
            # Main-agent sessions have exactly two segments ({prefix}.{agent})
            # and were already registered in the pool_agent_names loop above.
            # Subagent invocations have three segments ({prefix}.{agent}.{inv})
            # and must always be registered — even when the invocation_id
            # coincidentally matches a pool agent name, which would confuse
            # ``SessionInfo.from_str``'s rightmost-segment parsing.
            is_main_agent_session = (
                sub_sid.count(".") == 1 and sub_agent_name in self._pool_agent_names
            )
            if is_main_agent_session:
                continue
            if self._input.get_delta_queue(sub_sid) is None:
                self._input.register_connection(sub_sid, ws)
                state.attached_sessions.append(sub_sid)
                state.forward_tasks.append(asyncio.create_task(self._forward_deltas(sub_sid, ws)))

        # Also register subagent sessions from relation store — these may have
        # been dispatched but not yet written to transcript.
        attach_store = await self._session_store_for(attach_index_dir)
        if attach_store is not None:
            for parent_sid in list(state.attached_sessions):
                for child_session in await attach_store.get_children(parent_sid):
                    child_sid = str(child_session)
                    if self._input.get_delta_queue(child_sid) is None:
                        self._input.register_connection(child_sid, ws)
                        state.attached_sessions.append(child_sid)
                        state.forward_tasks.append(
                            asyncio.create_task(self._forward_deltas(child_sid, ws))
                        )

        # Watch for dynamically-created subagent delta queues (created by
        # send_envelope auto-create).  When a new queue appears for a
        # session_id not yet forwarded, start a _forward_deltas task.
        state.forward_tasks.append(asyncio.create_task(self._watch_new_queues(ws, state)))

        state.forward_tasks.append(asyncio.create_task(self._forward_deltas(session_id, ws)))

        att_agent = await self._resolve_agent(session_id, index_dir=attach_index_dir)
        await _safe_send_json(
            ws,
            DeltaEnvelope(
                session_id=session_id,
                agent_name=att_agent,
                event_type=WebUIEventType.ATTACHED.value,
                pool=self._pool_of_agent(att_agent),
            ).to_dict(),
        )

    async def _materialize_deferred_session(
        self, session_id: str, index_dir: Path | None = None
    ) -> None:
        """Persist a deferred (uuid_prefix-prefixed) session on first message.

        Attach creates a provisional id ``{uuid_prefix}.{agent}`` without
        persisting; this materializes it just before the pipeline writes the
        transcript, using ``create_with_prefix`` so ``uuid_prefix`` is
        the verbatim session_prefix — same id, no re-encoding.  Already-persisted
        sessions (reattach, existing conversations) are a no-op. *index_dir*
        scopes the record to the message's workspace session index.
        """
        if self._session_factory is None:
            return
        store = (
            await self._session_store_for(index_dir)
            if index_dir is not None
            else self._session_store
        )
        if store is None:
            return
        if await store.get(session_id) is not None:
            return  # already persisted
        session_prefix = session_id_prefix_of(session_id)
        agent = agent_of(session_id, default="unknown")
        session = self._session_factory.create_with_prefix(
            agent_name=agent,
            prefix=session_prefix,
        )
        if session.session_id != session_id:
            # Fallback: session_prefix contained a separator or was empty; persist a
            # from_str record so the session list still shows the conversation.
            session = SessionInfo.from_str(session_id)
        await store.save(session)

    async def _ws_send_message(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
        state: _WsConnectionState,
    ) -> None:
        session_id = str(data.get("session_id", ""))
        content = str(data.get("content", ""))
        request_id = str(data.get("_request_id", ""))
        # An attachment-only send (no text) is valid — the frontend enables Send
        # when there are pending uploads even with empty text (ADR-0013: a file
        # in a conversation is itself the message). Drop only when there is
        # neither text nor any attachment payload.
        has_attachment_payload = (
            isinstance(data.get("attachments"), list) and len(data.get("attachments") or []) > 0
        )
        if "." not in session_id or (not content and not has_attachment_payload):
            return

        # Resolve the target workspace ("ws" == workspace) from the payload up
        # front: every per-workspace store/index call below needs it. Empty ws
        # means the home workspace. Route the bound workspace root through the
        # SAME resolver the read paths use, so a message written here is always
        # read back from the same workspace.
        ws_raw = str(data.get("ws", ""))
        index_dir = self._index_dir_of_ws(ws_raw)
        workspace_path = self._ws_root_of(ws_raw)

        # Materialize a deferred draft (created via uuid_prefix+pool attach)
        # on its first message so the session enters the index before the
        # pipeline writes the transcript. Empty drafts are never persisted.
        await self._materialize_deferred_session(session_id, index_dir=index_dir)

        # NOTE: DO NOT call _try_intercept_control here.
        # Control slash commands (/pwd, /cd, /exit, /stop) are handled by
        # the IM pipeline (S2 EnvironmentControlStage / S3 SessionControlStage).
        # The WebUI does NOT need these — the workspace panel and sidebar
        # controls already provide the same functionality visually.
        # In WebUI, /pwd etc. correctly reach S6 (SkillParseStage) which
        # rejects them with "builtin_not_supported". That is intentional.

        resolved = await self._resolve_session(session_id, index_dir=index_dir)
        uuid_prefix = resolved.session_id_prefix
        explicit_agent = resolved.agent_name

        # Pool resolution is OWNED by S5 (ResolvePoolStage) — it also persists
        # the UI choice into PoolSessionStore so PoolRouter routes correctly.
        # The entry only hands the UI-selected pool (derived from the
        # session_id's agent segment) as explicit_pool; no inline resolution,
        # no _pool_switch_callback call here. (attach still uses the callback.)
        # For main agents the agent name IS the pool name; fall back to
        # explicit_agent directly when agent_pool_map lacks the entry (edge
        # case: map not yet populated during early server startup).
        explicit_pool = (
            (self._agent_pool_map.get(explicit_agent) or explicit_agent) if explicit_agent else None
        )

        # The session was already established upstream (attach / create_session).
        # Pass it through so the pipeline reuses session.session_id verbatim
        # instead of re-encoding the session_prefix (which would break
        # transcript/pool keying).  Reuse the already-resolved SessionInfo
        # from above (same args) rather than resolving a second time.
        pre_resolved = resolved

        # Run the WebUI sub-pipeline (S4..S8).
        from bot.input_pipeline.stages.resolve_pool import RoutingMeta
        from modex_agent.input_pipeline.envelope import AttachmentRef, UserInputEnvelope

        # Build AttachmentRefs from the client payload so uploaded files (POSTed
        # to the upload endpoint, returning {local_path, filename, mime?}) are
        # NOT orphaned — the ingest stage (G3) reads envelope.attachments and
        # would no-op on an empty list. Mirrors the QQ adapter
        # (bot/adapters/qq.py: attachments=[AttachmentRef(local_path=p) ...]).
        #
        # C1: the upload endpoint is the ONLY legitimate writer to the staging
        # dir, so an accepted local_path MUST resolve under it. A client could
        # otherwise point local_path at ANY server-readable file (e.g.
        # /etc/shadow, or a path under another workspace's data dir) and have
        # the ingest stage copy its bytes into the media store — making them
        # agent-perceivable and downloadable (path traversal / exfiltration).
        # The QQ adapter is unaffected (it builds the ref server-side).
        raw_attachments = data.get("attachments")
        attachments: list[AttachmentRef] = []
        if isinstance(raw_attachments, list):
            # Resolve the staging pool the SAME way the upload endpoint does
            # (_pool_of_agent -> _pool_for_agent_name, incl. dynamic-subagent
            # prefix matching). ``explicit_pool`` (agent_pool_map.get or the raw
            # agent name) diverges for subagent-instance sessions and would drop
            # a legitimately-uploaded file whose temp path lives under the
            # template pool's ``_tmp`` — the same file the upload endpoint wrote.
            staging_pool = (
                self._pool_of_agent(explicit_agent) if explicit_agent else _DEFAULT_AGENT_NAME
            )
            staging_root = self._media_tmp_dir_of_ws(ws_raw, staging_pool).resolve()
            for entry in raw_attachments:
                if not isinstance(entry, dict):
                    continue
                local_path = entry.get("local_path")
                if not local_path or not isinstance(local_path, str):
                    continue
                # Resolve before the containment check so symlinks / ``..``
                # segments cannot escape the staging dir.
                try:
                    resolved = Path(local_path).resolve()
                except (OSError, ValueError) as exc:
                    logger.warning(
                        "Dropping WS attachment %r: path unresolvable (%s)",
                        local_path,
                        exc,
                    )
                    continue
                if not resolved.is_relative_to(staging_root):
                    logger.warning(
                        "Dropping WS attachment %r: outside staging dir %s "
                        "(path-traversal rejection)",
                        local_path,
                        staging_root,
                    )
                    continue
                attachments.append(
                    AttachmentRef(
                        local_path=local_path,
                        filename=entry.get("filename")
                        if isinstance(entry.get("filename"), str)
                        else None,
                        mime_type=entry.get("mime") if isinstance(entry.get("mime"), str) else None,
                    )
                )

        envelope = UserInputEnvelope(
            external_id=uuid_prefix,
            content=content,
            channel="websocket",
            explicit_pool=explicit_pool,
            pre_resolved_session=pre_resolved,
            attachments=attachments,
        )
        envelope.metadata[RoutingMeta.WORKSPACE] = str(workspace_path)
        # Thread the UI-selected provider/model into the envelope so
        # ModelChoiceStage (WebUI-only) reads them off the metadata.
        provider_name = data.get("provider_name")
        model_name = data.get("model_name")
        if provider_name:
            envelope.metadata[RoutingMeta.MODEL_PROVIDER] = str(provider_name)
        if model_name:
            envelope.metadata[RoutingMeta.MODEL_MODEL] = str(model_name)
        result = await self._input_pipeline.handle(envelope, self._input_ctx)

        if result.should_continue():
            # Echo the user message back to the WS client so the frontend
            # can reconcile its optimistic message.
            final = result.envelope()
            full_sid = final.metadata[RoutingMeta.FULL_SESSION_ID]
            agent_name = final.metadata[RoutingMeta.RESOLVED_AGENT]
            pool_name = final.metadata[RoutingMeta.RESOLVED_POOL]
            from bot.webui.events import UserMessageEvent

            event = UserMessageEvent(
                session_id=full_sid,
                agent_name=agent_name,
                content=content,
                # Mirror persist_user_message.py:43 — carry the resolved
                # Attachment records so the sender's own attachments render on
                # their optimistic message mid-session, not only after a
                # transcript reload. resolved_attachments may be None/empty for
                # legacy messages; guard with ``or []``.
                attachments=[a.to_dict() for a in (final.resolved_attachments or [])],
            )
            meta: dict[str, object] = {}
            if request_id:
                meta["_request_id"] = request_id
            await _safe_send_json(
                ws, DeltaEnvelope.from_event(event, meta, pool=pool_name).to_dict()
            )
        else:
            # Terminate: pipeline consumed the message (e.g. /cd /pwd /exit
            # in WebUI chat which has no S2/S3, or unknown /skill).
            # Surface the reason to the client as an error envelope.
            response = result.response
            message = ""
            if response is not None:
                try:
                    message = str(response["message"])
                except (KeyError, TypeError):
                    pass
            pool = explicit_pool or _DEFAULT_AGENT_NAME
            await _safe_send_json(
                ws,
                DeltaEnvelope(
                    session_id=session_id,
                    agent_name=explicit_agent or _DEFAULT_AGENT_NAME,
                    event_type=WebUIEventType.ERROR.value,
                    pool=pool,
                    payload={"message": message or "unsupported command in WebUI chat"},
                ).to_dict(),
            )

    async def _ws_delete_conversation(
        self,
        ws: web.WebSocketResponse,
        data: dict[str, object],
    ) -> None:
        """Delete a conversation's full cascade (delegates to the collector).

        Mirrors the REST delete handler: resolves ws root + pool and delegates to
        the SessionGarbageCollector, which removes the root's record synchronously
        and drains the cascade + ten artifact types via the background pool.
        Supersedes the old prefix-based delete (subagents carry their own prefix,
        so prefix-delete missed the cascade — ADR-0018).
        """
        session_id = str(data.get("session_id", ""))
        if "." not in session_id:
            return
        ws_raw = str(data.get("ws", ""))
        index_dir = self._index_dir_of_ws(ws_raw)
        resolved = await self._resolve_session(session_id, index_dir=index_dir)
        agent_name = resolved.agent_name
        pool = self._pool_of_agent(agent_name)
        if self._session_gc is not None:
            await self._session_gc.delete_session_tree(
                session_id, ws_root=self._ws_root_of(ws_raw), pool=pool
            )
        else:
            logger.warning(
                "delete_conversation: no SessionGarbageCollector wired; skipping "
                "cascade deletion for %s",
                session_id,
            )
        await _safe_send_json(
            ws,
            DeltaEnvelope(
                session_id=session_id,
                agent_name=agent_name,
                event_type=WebUIEventType.CONVERSATION_DELETED.value,
                pool=pool,
            ).to_dict(),
        )

    # ------------------------------------------------------------------
    # Delta forwarding
    # ------------------------------------------------------------------

    async def _forward_deltas(self, session_id: str, ws: web.WebSocketResponse) -> None:
        """Background task: read DeltaEnvelopes and send as structured JSON."""
        try:
            q = self._input.get_delta_queue(session_id)
            if q is None:
                return
            while True:
                envelope: DeltaEnvelope = await q.get()
                await ws.send_json(envelope.to_dict())
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            logger.exception("Delta forwarding error for session %s", session_id)

    @staticmethod
    def _queue_belongs_to_connection(attached_sessions: list[str], session_id: str) -> bool:
        """True if *session_id*'s conversation is already owned by this connection.

        Convergence point for ws isolation on the shared WebSocket adapter: the
        adapter multiplexes every workspace/tab through one set of delta queues,
        keyed only by session id. A dynamically-created subagent queue
        (``{conv}.{agent}.{inv}``) belongs to whichever connection attached that
        conversation. We derive that from the connection's own
        ``attached_sessions`` — every attached session shares one conversation
        prefix — so no per-connection ws bookkeeping is needed: claim a queue
        only when its prefix matches a conversation this connection already owns.
        """
        prefix = session_id_prefix_of(session_id)
        return any(session_id_prefix_of(s) == prefix for s in attached_sessions)

    async def _watch_new_queues(self, ws: web.WebSocketResponse, state: _WsConnectionState) -> None:
        """Periodically check for dynamically-created delta queues and start
        forwarding tasks for any that are not yet being drained.

        Subagent sessions dispatched after the initial attach have their delta
        queues auto-created by ``send_envelope``, but no ``_forward_deltas``
        task is running for them.  This watcher discovers those queues and
        starts forwarding.

        ws-scoped: only queues whose conversation this connection already owns
        are claimed (see :meth:`_queue_belongs_to_connection`), so a subagent
        stream from one workspace/tab is never bound to another connection.
        """
        try:
            while True:
                await asyncio.sleep(1.0)
                if state._stopped:
                    # cleanup() has started: stop claiming queues so we never
                    # append a session / spawn a task that cleanup just cleared.
                    break
                for session_id in list(self._input._delta_queues):
                    if state._stopped:
                        break
                    if session_id in state.attached_sessions:
                        continue
                    if not self._queue_belongs_to_connection(state.attached_sessions, session_id):
                        # Belongs to another connection's conversation; let that
                        # connection's own watcher claim it.
                        continue
                    state.attached_sessions.append(session_id)
                    state.forward_tasks.append(
                        asyncio.create_task(self._forward_deltas(session_id, ws))
                    )
        except (asyncio.CancelledError, ConnectionError):
            pass
        except Exception:
            logger.exception("Queue watcher error")
