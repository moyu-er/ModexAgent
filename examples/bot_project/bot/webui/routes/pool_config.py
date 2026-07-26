"""aiohttp route handlers for the pool / MCP / skills / prompts REST API.

Thin adapters extracted from :class:`bot.webui.server.WebUIServer`. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` (set by :func:`register_pool_config_routes` —
itself called from :meth:`WebUIServer._setup_routes`), matching the
``control_facade`` pattern in :mod:`bot.control.routes` and the
:mod:`bot.webui.routes.models` / :mod:`bot.webui.routes.sessions` /
:mod:`bot.webui.routes.workspace` convention.

Routes registered:
    GET    /api/pools                                          -- list pool summaries.
    POST   /api/pools                                          -- create a pool.
    GET    /api/pools/{pool}                                   -- read one pool tree.
    PUT    /api/pools/{pool}                                   -- validate + persist a pool tree.
    DELETE /api/pools/{pool}                                   -- delete a pool.
    POST   /api/pools/{pool}/peers                             -- add a bidirectional peer edge.
    DELETE /api/pools/{pool}/peers/{peer}                      -- remove a bidirectional peer edge.
    GET    /api/mcp                                            -- read the typed MCP registry mapping.
    POST   /api/mcp/{server}                                   -- insert or update one MCP server entry.
    PUT    /api/mcp/{server}                                   -- insert or update one MCP server entry.
    DELETE /api/mcp/{server}                                   -- remove one MCP server (refuses if referenced).
    GET    /api/skills                                         -- list global skills.
    POST   /api/skills                                         -- upload a global skill (multipart or JSON).
    DELETE /api/skills/{name}                                  -- remove a global skill.
    GET    /api/pools/{pool}/agents/{agent}/skills             -- list an agent's skills.
    POST   /api/pools/{pool}/agents/{agent}/skills/{name}      -- assign a skill copy.
    DELETE /api/pools/{pool}/agents/{agent}/skills/{name}      -- remove a skill copy.
    GET    /api/prompts                                        -- list agent prompt md files.
    POST   /api/prompts                                        -- create a new prompt md.
    GET    /api/prompts/{name}                                 -- read one prompt md WITHOUT seeding.
    PUT    /api/prompts/{name}                                 -- upsert the prompt md for ``name``.
    DELETE /api/prompts/{name}                                 -- delete a prompt md if unreferenced.

Helpers (:func:`pool_cfg_required`, :func:`upload_skill_multipart`,
:func:`upload_skill_json`) are also extracted here; the handlers access
``server._pool_config_controller`` (kept on :class:`WebUIServer`).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import web
from pydantic import ValidationError

from bot.config.prompt_store import PromptExistsError
from bot.service.config_controller import FieldValidationError
from bot.service.pool_config_controller import (
    PoolNotEmptyError,
    PromptInUseError,
)
from bot.webui.types import (
    _SKILL_MAX_FILE_BYTES,
    _SKILL_MAX_FILE_MB,
    _SKILL_MAX_TOTAL_BYTES,
    _SKILL_MAX_TOTAL_MB,
    _SkillUploadFallback,
    _skill_relpath,
)

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


# ── Helpers ─────────────────────────────────────────────────────────────────


def pool_cfg_required(server: WebUIServer) -> web.Response | None:
    """Return a 503 response if no PoolConfigController is wired, else None."""
    if server._pool_config_controller is None:
        return web.json_response({"error": "pool config not configured"}, status=503)
    return None


# ── Pools ───────────────────────────────────────────────────────────────────


async def handle_list_pools(request: web.Request) -> web.Response:
    """GET /api/pools -- list pool summaries."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        pools = server._pool_config_controller.list_pools()
    except Exception:  # noqa: BLE001
        logger.exception("list_pools failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response([p.model_dump(mode="json") for p in pools])


async def handle_create_pool(request: web.Request) -> web.Response:
    """POST /api/pools -- create a pool. Body: {"name": "<pool>"}."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
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
        tree = server._pool_config_controller.create_pool(name)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("create_pool failed")
        return web.json_response({"error": "create failed"}, status=500)
    return web.json_response(tree.model_dump(mode="json"))


async def handle_read_pool(request: web.Request) -> web.Response:
    """GET /api/pools/{pool} -- read one pool tree."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    try:
        tree = server._pool_config_controller.read_pool(pool)
    except KeyError:
        return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("read_pool failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response(tree.model_dump(mode="json"))


async def handle_write_pool(request: web.Request) -> web.Response:
    """PUT /api/pools/{pool} -- validate + persist a pool tree. Body = PoolTree."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
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
        written = server._pool_config_controller.write_pool(pool, tree)
    except KeyError:
        return web.json_response({"error": f"unknown pool: {pool}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("write_pool failed")
        return web.json_response({"error": "write failed"}, status=500)
    return web.json_response(written.model_dump(mode="json"))


async def handle_delete_pool(request: web.Request) -> web.Response:
    """DELETE /api/pools/{pool} -- delete a pool."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    try:
        server._pool_config_controller.delete_pool(pool)
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


async def handle_add_peer(request: web.Request) -> web.Response:
    """POST /api/pools/{pool}/peers -- add a bidirectional peer edge.

    Body: {"peer": "<other_pool>"}. On success both sides of the edge
    are written and both updated pool trees are returned so the UI can
    refresh the current pool and any visible peer pool.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
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
        tree_a, tree_b = server._pool_config_controller.add_peer(pool, peer)
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


async def handle_remove_peer(request: web.Request) -> web.Response:
    """DELETE /api/pools/{pool}/peers/{peer} -- remove a bidirectional peer edge.

    Both sides of the edge are removed atomically. Returns both updated
    pool trees.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    pool = request.match_info["pool"]
    peer = request.match_info["peer"]
    try:
        tree_a, tree_b = server._pool_config_controller.remove_peer(pool, peer)
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


# ── MCP ─────────────────────────────────────────────────────────────────────


async def handle_read_mcp(request: web.Request) -> web.Response:
    """GET /api/mcp -- read the typed MCP registry mapping."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        registry = server._pool_config_controller.read_mcp()
    except Exception:  # noqa: BLE001
        logger.exception("read_mcp failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response(
        {name: e.model_dump(mode="json", by_alias=True) for name, e in registry.items()}
    )


async def handle_upsert_mcp(request: web.Request) -> web.Response:
    """POST/PUT /api/mcp/{server} -- insert or update one MCP server entry."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["server"]
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("upsert_mcp: bad JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    try:
        entry = server._pool_config_controller.upsert_mcp(name, body)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("upsert_mcp failed")
        return web.json_response({"error": "write failed"}, status=500)
    return web.json_response(entry.model_dump(mode="json", by_alias=True))


async def handle_delete_mcp(request: web.Request) -> web.Response:
    """DELETE /api/mcp/{server} -- remove one MCP server (refuses if referenced)."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["server"]
    try:
        server._pool_config_controller.delete_mcp(name)
    except KeyError:
        return web.json_response({"error": f"unknown server: {name}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("delete_mcp failed")
        return web.json_response({"error": "delete failed"}, status=500)
    return web.json_response({"deleted": name})


# ── Skills ──────────────────────────────────────────────────────────────────


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


async def upload_skill_multipart(
    request: web.Request, server: WebUIServer
) -> web.Response:
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
        entry = server._pool_config_controller.upload_skill(name, file_tree)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("upload_skill failed")
        return web.json_response({"error": "upload failed"}, status=500)
    return web.json_response(entry.model_dump(mode="json"))


async def upload_skill_json(
    request: web.Request, server: WebUIServer
) -> web.Response:
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


# ── Prompts ─────────────────────────────────────────────────────────────────


async def handle_list_prompts(request: web.Request) -> web.Response:
    """GET /api/prompts -- list agent prompt md files (name/size/mtime)."""
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    try:
        prompts = server._pool_config_controller.list_prompts()
    except Exception:  # noqa: BLE001
        logger.exception("list_prompts failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response([p.model_dump(mode="json") for p in prompts])


async def handle_read_prompt_strict(request: web.Request) -> web.Response:
    """GET /api/prompts/{name} -- read one prompt md WITHOUT seeding.

    Returns 404 when the file is absent (does not call ``read_or_seed``);
    returns 400 on a malformed name.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["name"]
    try:
        prompt = server._pool_config_controller.read_prompt_strict(name)
    except KeyError:
        return web.json_response({"error": f"unknown prompt: {name}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("read_prompt_strict failed")
        return web.json_response({"error": "read failed"}, status=500)
    return web.json_response(prompt.model_dump(mode="json"))


async def handle_write_prompt_global(request: web.Request) -> web.Response:
    """PUT /api/prompts/{name} -- upsert the prompt md for ``name``.

    Reuses :meth:`PoolConfigController.write_prompt` (atomic write + marks
    ``restart_required`` on the ``prompt`` artifact class). Creates the
    file if absent (upsert semantics — no 409 on existing names).
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
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
        prompt = server._pool_config_controller.write_prompt(name, content)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001
        logger.exception("write_prompt_global failed")
        return web.json_response({"error": "write failed"}, status=500)
    return web.json_response(prompt.model_dump(mode="json"))


async def handle_create_prompt(request: web.Request) -> web.Response:
    """POST /api/prompts -- create a new prompt md.

    Body: ``{"name": str, "content"?: str}``. Validates the name against the
    agent-name regex; rejects a duplicate name with HTTP 409 via
    :class:`PromptExistsError`. When ``content`` is omitted the seed text
    is :data:`PromptStore.DEFAULT_PROMPT_SEED`.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
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
        prompt = server._pool_config_controller.create_prompt(name, content)
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


async def handle_delete_prompt_global(request: web.Request) -> web.Response:
    """DELETE /api/prompts/{name} -- delete a prompt md if unreferenced.

    Returns 200 with ``{deleted: str}`` when unreferenced; removes the file.
    Returns 409 with ``{error: "in_use", usages: [...]}`` when any pool's
    main agent or subagent references the prompt (explicit ``prompt_name``
    match or the fallback case where ``prompt_name`` is empty and
    ``agent_name`` equals the prompt name). Returns 404 when the file does
    not exist. Does NOT remove the file on 409.
    """
    server: WebUIServer = request.app["server"]
    if (miss := pool_cfg_required(server)) is not None:
        return miss
    name = request.match_info["name"]
    try:
        server._pool_config_controller.delete_prompt(name)
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


# ── Registration ────────────────────────────────────────────────────────────


def register_pool_config_routes(server: WebUIServer) -> None:
    """Register the pool / MCP / skills / prompts routes on ``server.app.router``.

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
    app.router.add_get("/api/pools", handle_list_pools)
    app.router.add_post("/api/pools", handle_create_pool)
    app.router.add_get("/api/pools/{pool}", handle_read_pool)
    app.router.add_put("/api/pools/{pool}", handle_write_pool)
    app.router.add_delete("/api/pools/{pool}", handle_delete_pool)
    app.router.add_post("/api/pools/{pool}/peers", handle_add_peer)
    app.router.add_delete("/api/pools/{pool}/peers/{peer}", handle_remove_peer)
    app.router.add_get("/api/mcp", handle_read_mcp)
    app.router.add_post("/api/mcp/{server}", handle_upsert_mcp)
    app.router.add_put("/api/mcp/{server}", handle_upsert_mcp)
    app.router.add_delete("/api/mcp/{server}", handle_delete_mcp)
    app.router.add_get("/api/skills", handle_list_skills)
    app.router.add_post("/api/skills", handle_upload_skill)
    app.router.add_delete("/api/skills/{name}", handle_delete_skill)
    app.router.add_get("/api/prompts", handle_list_prompts)
    app.router.add_post("/api/prompts", handle_create_prompt)
    app.router.add_get("/api/prompts/{name}", handle_read_prompt_strict)
    app.router.add_put("/api/prompts/{name}", handle_write_prompt_global)
    app.router.add_delete("/api/prompts/{name}", handle_delete_prompt_global)
    app.router.add_get(
        "/api/pools/{pool}/agents/{agent}/skills", handle_list_agent_skills
    )
    app.router.add_post(
        "/api/pools/{pool}/agents/{agent}/skills/{name}",
        handle_assign_skill,
    )
    app.router.add_delete(
        "/api/pools/{pool}/agents/{agent}/skills/{name}",
        handle_unassign_skill,
    )


__all__ = [
    "handle_add_peer",
    "handle_assign_skill",
    "handle_create_pool",
    "handle_create_prompt",
    "handle_delete_mcp",
    "handle_delete_pool",
    "handle_delete_prompt_global",
    "handle_delete_skill",
    "handle_list_agent_skills",
    "handle_list_pools",
    "handle_list_prompts",
    "handle_list_skills",
    "handle_read_mcp",
    "handle_read_pool",
    "handle_read_prompt_strict",
    "handle_remove_peer",
    "handle_unassign_skill",
    "handle_upsert_mcp",
    "handle_upload_skill",
    "handle_write_pool",
    "handle_write_prompt_global",
    "pool_cfg_required",
    "register_pool_config_routes",
    "upload_skill_json",
    "upload_skill_multipart",
]
