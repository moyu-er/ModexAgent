"""Attachment handlers — upload / download / media config.

Extracted from the original :mod:`bot.webui.routes.sessions` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared helpers in
:mod:`bot.webui.routes.sessions`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from aiohttp import web

from bot.webui.types import _UPLOAD_CHUNK_BYTES
from modex_agent.core.session_id import session_id_prefix_of

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_download_attachment(request: web.Request) -> web.Response:
    """``GET /api/sessions/{session_id}/attachments/{attachment_id}?ws=<ws>``.

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


    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    attachment_id: str = request.match_info["attachment_id"]
    ws_raw = request.query.get("ws", "")
    sessions_dir = server._sessions_dir_of_ws(ws_raw)

    att = await find_attachment(server._store, session_id, attachment_id, sessions_dir=sessions_dir)
    if att is None:
        return web.Response(status=404, text="attachment not found")

    path: Path | None
    if att.locator is AttachmentLocator.MEDIA:
        server._index_dir_of_ws(ws_raw)
        session_prefix = session_id_prefix_of(session_id)
        pool = server._resolve_pool_for_request(request.query.get("pool"), session_prefix)
        media_store = server._input_ctx.media_store if server._input_ctx is not None else None
        if media_store is None:
            # No media resolver wired — cannot serve inbound bytes.
            return web.Response(status=404, text="attachment not found")
        media_dir = server._media_dir_of_ws(ws_raw, pool)
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


async def handle_media_config(request: web.Request) -> web.Response:
    """``GET /api/media/config`` -- expose MediaConfig limits for pre-validation.

    Returns the active ``MediaConfig`` numbers the frontend needs to
    pre-validate a selection before uploading (ADR-0013 §7). v1 is a single
    shared config (per-pool override is a later extension; the ingest stage
    reads the same instance off the input context). When no input context is
    wired (minimal tests), the frozen ``MediaConfig()`` defaults are
    returned so the endpoint always answers with the authoritative numbers.
    """
    from modex_agent.multi_agent.pool_config.media import MediaConfig

    server: WebUIServer = request.app["server"]
    config: MediaConfig = (
        server._input_ctx.media_config if server._input_ctx is not None else MediaConfig()
    )
    return web.json_response(
        {
            "max_image_bytes": config.max_image_bytes,
            "max_text_doc_bytes": config.max_text_doc_bytes,
            "session_budget_bytes": config.session_budget_bytes,
            "max_outbound_bytes": config.max_outbound_bytes,
        }
    )


async def handle_upload_attachment(request: web.Request) -> web.Response:
    """``POST /api/sessions/{session_id}/attachments`` -- temp-file receiver.

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


    server: WebUIServer = request.app["server"]
    session_id: str = request.match_info["session_id"]
    ws_raw = request.query.get("ws", "")

    server._index_dir_of_ws(ws_raw)
    session_prefix = session_id_prefix_of(session_id)
    pool = server._resolve_pool_for_request(request.query.get("pool"), session_prefix)

    reader = await request.multipart()
    part = await reader.next()
    if part is None or part.name != "file":
        return web.json_response({"error": "missing 'file' part"}, status=400)

    config = (
        server._input_ctx.media_config_for(pool) if server._input_ctx is not None else MediaConfig()
    )
    # Loose early cap: reject anything above the most generous accepted
    # limit. The authoritative per-kind gate runs in the ingest stage.
    early_cap = max(config.max_image_bytes, config.max_text_doc_bytes)

    tmp_dir = server._media_tmp_dir_of_ws(ws_raw, pool)
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
