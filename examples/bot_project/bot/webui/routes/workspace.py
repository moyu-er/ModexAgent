"""aiohttp route handlers for the workspace REST API.

Thin adapters extracted from :class:`bot.webui.server.WebUIServer`. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` (set by :func:`register_models_routes` — itself
called earlier from :meth:`WebUIServer._setup_routes`), matching the
``control_facade`` pattern in :mod:`bot.control.routes` and the
:mod:`bot.webui.routes.models` / :mod:`bot.webui.routes.sessions` convention.

Routes registered:
    GET  /api/workspace         -- home path, recent workspaces, and timezone.
    POST /api/workspace/cd      -- change current workspace directory.
    POST /api/workspace/pick    -- open OS-native folder picker and switch.
    GET  /api/workspace/recent  -- recently visited workspace paths.

Helpers (:func:`media_dir_of_ws`, :func:`media_tmp_dir_of_ws`,
:func:`known_workspace_data_roots`, :func:`clear_dir_contents`,
:func:`sweep_media_tmp_orphans`) are also extracted here;
:class:`WebUIServer` keeps thin delegates for the cross-module callers
(:mod:`bot.webui.routes.sessions` reads ``server._media_dir_of_ws`` /
``server._media_tmp_dir_of_ws``; :class:`bot.service.web_ui_service.WebUIService`
and ``tests/webui/test_server_attachment_endpoints.py`` call
``server.sweep_media_tmp_orphans()``).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.types import _PICKER_SCRIPT, _PICKER_TIMEOUT_S
from modex_agent.utils.timezone import get_user_timezone
from modex_agent.workspace.paths import WorkspacePaths

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


# ── Helpers ─────────────────────────────────────────────────────────────────


def media_dir_of_ws(server: WebUIServer, ws_raw: str, pool: str) -> Path:
    """Resolve the raw ws path to the pool's MEDIA directory.

    Mirrors :func:`sessions_dir_of_ws` and the business
    :class:`WorkspaceScopedMediaStore` ctxvar resolution
    (``WorkspacePaths(root=<ws_root>/<data_dir>).media_dir(pool)``) so an
    inbound attachment written under a workspace is read back from the same
    workspace's media dir. Home (empty ``ws_raw``) resolves against the
    precomputed ``_home_sessions_dir`` parent so it never depends on
    ``_data_dir_name`` being set, exactly like the sessions reader.
    """
    if not ws_raw:
        return WorkspacePaths(root=server._home_sessions_dir.parent).media_dir(pool)
    try:
        return WorkspacePaths(
            root=server._ws_root_of(ws_raw) / server._data_dir_name
        ).media_dir(pool)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to build media dir for %r: %s", ws_raw, exc)
        return WorkspacePaths(root=server._home_sessions_dir.parent).media_dir(pool)


def media_tmp_dir_of_ws(server: WebUIServer, ws_raw: str, pool: str) -> Path:
    """Resolve the raw ws path to the pool's media ``_tmp`` directory.

    Staging area for the upload endpoint
    (:func:`bot.webui.routes.sessions.handle_upload_attachment`) — accepted
    files are re-persisted by the ingest stage into the real media dir, so
    temp files here are disposable. Resolved the same way as
    :func:`media_dir_of_ws` so a temp file written under a workspace is
    read back from the same workspace when the WS message flows through the
    pipeline. Leftover files from a previous run are reclaimed by
    :func:`sweep_media_tmp_orphans` at startup.
    """
    return media_dir_of_ws(server, ws_raw, pool) / "_tmp"


def known_workspace_data_roots(server: WebUIServer) -> list[Path]:
    """Distinct ``<root>/<data_dir>`` dirs for home + recent workspaces.

    Home's data root is ``_home_sessions_dir.parent`` (already encodes the
    data dir, so it resolves even before ``_data_dir_name`` is set). Each
    recent workspace resolves via :meth:`WebUIServer._ws_root_of` +
    ``_data_dir_name``; recent is skipped while ``_data_dir_name`` is unset
    (minimal test wiring).
    """
    roots: list[Path] = [server._home_sessions_dir.parent]
    if server._data_dir_name and server._recent_workspaces is not None:
        for entry in server._recent_workspaces.list_recent():
            ws_raw = str(entry.get("path", ""))
            if ws_raw:
                roots.append(server._ws_root_of(ws_raw) / server._data_dir_name)
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def clear_dir_contents(path: Path) -> None:
    """Remove every entry inside *path*, keeping the directory itself."""
    for entry in path.iterdir():
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            logger.warning("media/_tmp sweep: could not remove %s: %s", entry, exc)


def sweep_media_tmp_orphans(server: WebUIServer) -> None:
    """Delete leftover upload temp files from a previous run.

    The upload endpoint stages bytes under ``<data_dir>/media/<pool>/_tmp``;
    the ingest stage re-persists accepted files into the real media dir
    (``uploads/``). A file left in ``_tmp`` is an upload that never became a
    WS message (client disconnected, crash, etc.) — disposable. Sweep ``_tmp``
    across home + every recent workspace at startup so orphans do not
    accumulate on disk. Accepted files under ``uploads/`` are never touched.
    """
    for data_root in known_workspace_data_roots(server):
        media_dir = data_root / "media"
        if not media_dir.is_dir():
            continue
        # Only ``_tmp`` dirs one level under ``media/<pool>/`` — leaves the
        # ``uploads/`` subtree (accepted, budget-managed bytes) untouched.
        for tmp_dir in media_dir.glob("*/_tmp"):
            if tmp_dir.is_dir():
                clear_dir_contents(tmp_dir)


# ── Handlers ────────────────────────────────────────────────────────────────


async def handle_workspace(request: web.Request) -> web.Response:
    """``GET /api/workspace`` -- return home path, recent workspaces, and timezone."""
    server: WebUIServer = request.app["server"]
    home = str(server._workspace_control.home) if server._workspace_control is not None else ""
    recent: list[dict[str, object]] = []
    if server._recent_workspaces is not None:
        recent = [
            {"path": r.get("path")}
            for r in server._recent_workspaces.list_recent()
            if isinstance(r, dict) and "path" in r
        ]
    return web.json_response(
        {"home": home, "recent": recent, "timezone": str(get_user_timezone())}
    )


async def handle_workspace_cd(request: web.Request) -> web.Response:
    """``POST /api/workspace/cd`` -- change current workspace directory."""
    server: WebUIServer = request.app["server"]
    if server._workspace_control is None:
        return web.json_response(
            {"success": False, "cwd": "", "notice": "Workspace not configured"},
            status=503,
        )
    target: str = ""
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to parse workspace/cd JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    if isinstance(body, dict):
        raw = body.get("path", "")
        if isinstance(raw, str):
            target = raw.strip()
    if not target:
        target = str(server._workspace_control.home)
    result = await server._workspace_control.open_workspace(
        target
    )  # registers the workspace without mutating the agent_pool_map
    if result.success and server._recent_workspaces is not None:
        server._recent_workspaces.add(str(result.current_path))
    return web.json_response(
        {
            "success": result.success,
            "cwd": str(result.current_path),
            "notice": result.notice,
        }
    )


async def handle_workspace_pick(request: web.Request) -> web.Response:
    """``POST /api/workspace/pick`` -- open the OS-native folder picker and switch.

    Combines folder selection and workspace switching into a single
    request so the frontend doesn't pay two HTTP round-trips. Uses
    ``asyncio.create_subprocess_exec`` (not ``to_thread + subprocess.run``)
    to manage the picker subprocess directly in the event loop, avoiding
    a redundant worker-thread hop.

    Responses:
    - ``200 {"path": "...", "success": true, "cwd": "..."}``  — picked + switched
    - ``200 {"path": null, "success": false}``               — user cancelled
    - ``503 {"error": "..."}``                                — picker unavailable
    - ``504 {"error": "..."}``                                — picker timed out
    """
    server: WebUIServer = request.app["server"]
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _PICKER_SCRIPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_PICKER_TIMEOUT_S
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.info("Native directory picker timed out after %ds", _PICKER_TIMEOUT_S)
        return web.json_response(
            {"error": "Directory picker timed out."}, status=504
        )
    except Exception as exc:  # noqa: BLE001
        logger.info("Native directory picker unavailable: %s", exc)
        return web.json_response(
            {"error": "Directory picker is not available in this environment."},
            status=503,
        )

    if proc.returncode != 0:
        err = stderr_bytes.decode(errors="replace").strip() if stderr_bytes else "picker failed"
        logger.info("Native directory picker failed: %s", err)
        return web.json_response(
            {"error": "Directory picker is not available in this environment."},
            status=503,
        )

    picked = stdout_bytes.decode(errors="replace").strip()

    if not picked:
        return web.json_response({"path": None, "success": False})

    if server._workspace_control is None:
        return web.json_response(
            {"path": picked, "success": False, "notice": "Workspace not configured"},
        )

    try:
        result = await server._workspace_control.open_workspace(picked)
    except Exception as exc:  # noqa: BLE001
        logger.warning("workspace/pick: open_workspace failed: %s", exc)
        return web.json_response(
            {"path": picked, "success": False, "notice": str(exc)},
        )

    if result.success and server._recent_workspaces is not None:
        server._recent_workspaces.add(str(result.current_path))

    return web.json_response(
        {
            "path": str(result.current_path),
            "success": result.success,
            "cwd": str(result.current_path),
            "notice": result.notice,
        }
    )


async def handle_workspace_recent(request: web.Request) -> web.Response:
    """``GET /api/workspace/recent`` -- return recently visited workspace paths."""
    server: WebUIServer = request.app["server"]
    if server._recent_workspaces is None:
        return web.json_response({"recent": []})
    return web.json_response(
        {
            "recent": server._recent_workspaces.list_recent(),
        }
    )


# ── Registration ────────────────────────────────────────────────────────────


def register_workspace_routes(server: WebUIServer) -> None:
    """Register the workspace routes on ``server.app.router``.

    ``app["server"]`` is set by :func:`bot.webui.routes.models.register_models_routes`
    (called earlier from :meth:`WebUIServer._setup_routes`); this function
    relies on that slot being present so the route handlers can reach server
    state.

    Called from :meth:`WebUIServer._setup_routes`.
    """
    app = server.app
    # ``app["server"]`` is set by register_models_routes; assert defensively
    # so a future reordering surfaces a clear error rather than a KeyError
    # inside a request handler.
    if "server" not in app:
        app["server"] = server
    app.router.add_get("/api/workspace", handle_workspace)
    app.router.add_post("/api/workspace/cd", handle_workspace_cd)
    app.router.add_post("/api/workspace/pick", handle_workspace_pick)
    app.router.add_get("/api/workspace/recent", handle_workspace_recent)


__all__ = [
    "clear_dir_contents",
    "handle_workspace",
    "handle_workspace_cd",
    "handle_workspace_pick",
    "handle_workspace_recent",
    "known_workspace_data_roots",
    "media_dir_of_ws",
    "media_tmp_dir_of_ws",
    "register_workspace_routes",
    "sweep_media_tmp_orphans",
]
