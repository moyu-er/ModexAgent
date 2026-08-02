"""Skills route handlers — global library + per-agent assignment.

Extracted from the original :mod:`bot.webui.routes.pool_config` module. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` and delegates to the shared :func:`pool_cfg_required`
guard in :mod:`bot.webui.routes.pool_config`.

The shared file-tree building logic (per-file + total size caps, normalized
relpath accumulation) is deduplicated into
:func:`bot.webui.routes.pool_config._materialize_skill_files` — the ONE
micro-convergence between :func:`upload_skill_multipart` and
:func:`upload_skill_json`. Handler logic (multipart parsing, JSON body
validation, name inference, fallback raising) is preserved verbatim.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web

from bot.service.config_controller import FieldValidationError
from bot.webui.routes.pool_config import (
    _materialize_skill_files,
    pool_cfg_required,
)
from bot.webui.types import (
    _skill_relpath,
    _SkillUploadFallback,
)

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


async def handle_list_skills(request: web.Request) -> web.Response:
    """GET /api/skills -- list global skills."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        skills = server._pool_config_controller.list_skills()
    except Exception:  # noqa: BLE001
        logger.exception("list_skills failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response([s.model_dump(mode="json") for s in skills])


async def handle_upload_skill(request: web.Request) -> web.Response:
    """POST /api/skills -- upload a global skill.

    Accepts multipart/form-data (preferred, matches the frontend
    ``webkitdirectory`` upload): each part's filename is a path under
    ``<skillName>/...``; keys are normalized relative to ``<skillName>/``.
    A text ``name`` form field overrides the skill-name inference from the
    path prefix. Per-file and total-size caps reject oversized uploads.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    ct = request.content_type or ""
    if ct.startswith("multipart/"):
        return await upload_skill_multipart(request, server)
    return await upload_skill_json(request, server)


async def upload_skill_multipart(request: web.Request, server: WebUIServer) -> web.Response:
    name: str | None = None
    items: list[tuple[str, bytes, str]] = []
    try:
        reader = await request.multipart()
    except Exception as exc:  # noqa: BLE001 - not multipart / parser error
        logger.debug("skill upload: multipart unavailable (%s) -- falling back", exc)
        raise _SkillUploadFallback() from exc
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
        if not isinstance(data, bytes | bytearray):
            continue
        data = bytes(data)
        rel = _skill_relpath(filename)
        if rel is not None:
            items.append((rel, data, filename))
    if name is None:
        # Infer the skill name from the first path segment if present.
        for rel, _, _ in items:
            head = rel.split("/", 1)[0]
            if head and head != rel:
                name = head
                break
    if not name:
        return web.json_response(
            {"error": "validation", "fields": {"name": ["required"]}}, status=400
        )
    if not items:
        raise _SkillUploadFallback()
    result = _materialize_skill_files(items)
    if isinstance(result, web.Response):
        return result
    file_tree: dict[str, bytes] = result
    try:
        entry = server._pool_config_controller.upload_skill(name, file_tree)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("upload_skill failed")
        return web.json_response({"error": "upload failed"}, status=500)
    return web.json_response(entry.model_dump(mode="json"))


async def upload_skill_json(request: web.Request, server: WebUIServer) -> web.Response:
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

    items: list[tuple[str, bytes, str]] = []
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
        items.append((rel, data, rel_b64))
    result = _materialize_skill_files(items)
    if isinstance(result, web.Response):
        return result
    file_tree: dict[str, bytes] = result
    try:
        entry = server._pool_config_controller.upload_skill(name, file_tree)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("upload_skill_json failed")
        return web.json_response({"error": "upload failed"}, status=500)
    return web.json_response(entry.model_dump(mode="json"))


async def handle_delete_skill(request: web.Request) -> web.Response:
    """DELETE /api/skills/{name} -- remove a global skill (per-agent copies stay)."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["name"]
    try:
        server._pool_config_controller.delete_skill(name)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("delete_skill failed")
        return web.json_response({"error": "delete failed"}, status=500)
    return web.json_response({"deleted": name})


async def handle_list_agent_skills(request: web.Request) -> web.Response:
    """GET /api/pools/{pool}/agents/{agent}/skills -- list an agent's skills."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    agent = request.match_info["agent"]
    try:
        skills = server._pool_config_controller.list_agent_skills(pool, agent)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("list_agent_skills failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response([s.model_dump(mode="json") for s in skills])


async def handle_assign_skill(request: web.Request) -> web.Response:
    """POST /api/pools/{pool}/agents/{agent}/skills/{name} -- assign a skill copy."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    agent = request.match_info["agent"]
    name = request.match_info["name"]
    try:
        server._pool_config_controller.assign_skill(pool, agent, name)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("assign_skill failed")
        return web.json_response({"error": "assign failed"}, status=500)
    return web.json_response({"assigned": name})


async def handle_unassign_skill(request: web.Request) -> web.Response:
    """DELETE /api/pools/{pool}/agents/{agent}/skills/{name} -- remove a skill copy."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    agent = request.match_info["agent"]
    name = request.match_info["name"]
    try:
        server._pool_config_controller.unassign_skill(pool, agent, name)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("unassign_skill failed")
        return web.json_response({"error": "unassign failed"}, status=500)
    return web.json_response({"unassigned": name})
