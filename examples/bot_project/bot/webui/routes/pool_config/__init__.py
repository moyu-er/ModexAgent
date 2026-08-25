"""aiohttp route handlers for the pool / MCP / skills / prompts REST API.

Thin adapters extracted from :class:`bot.webui.server.WebUIServer`. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` (set by :func:`register_pool_config_routes` —
itself called from :meth:`WebUIServer._setup_routes`), matching the
``control_facade`` pattern in :mod:`bot.control.routes` and the
:mod:`bot.webui.routes.models` / :mod:`bot.webui.routes.sessions` /
:mod:`bot.webui.routes.workspace` convention.

This package owns the pool / MCP / skills / prompts REST endpoints.
Sub-modules own the per-concern handlers:

- :mod:`bot.webui.routes.pool_config.pools` -- declared pool listing (read-only).
- :mod:`bot.webui.routes.pool_config.mcp` -- MCP registry read / upsert / delete.
- :mod:`bot.webui.routes.pool_config.skills` -- global skills + per-agent copies.
- :mod:`bot.webui.routes.pool_config.prompts` -- agent prompt md CRUD.

Pool trees are edited through the scope declaration editor
(``PUT /api/scope/declaration``, ticket 16); the legacy pool.yml CRUD
routes retired with the legacy roster road (ticket 11).

Routes registered:
    GET    /api/pools                                          -- list pool summaries.
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

Shared helpers (:func:`pool_cfg_required`, :func:`_materialize_skill_files`)
live in this module so every handler sub-module can import them; the
handlers access ``server._pool_config_controller`` (kept on
:class:`WebUIServer`).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from aiohttp import web

from bot.webui.types import (
    _SKILL_MAX_FILE_BYTES,
    _SKILL_MAX_FILE_MB,
    _SKILL_MAX_TOTAL_BYTES,
    _SKILL_MAX_TOTAL_MB,
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


def _materialize_skill_files(
    items: Iterable[tuple[str, bytes, str]],
) -> dict[str, bytes] | web.Response:
    """Build the ``{relpath: bytes}`` file tree with per-file + total size caps.

    Shared by :func:`bot.webui.routes.pool_config.skills.upload_skill_multipart`
    and :func:`bot.webui.routes.pool_config.skills.upload_skill_json` (the ONE
    allowed micro-convergence — deduplicates the size-cap + accumulation + store
    loop that was previously copy-pasted between the two upload paths).

    Each item is a ``(rel, data, raw_label)`` tuple where ``rel`` is the
    normalized relpath (already passed through :func:`_skill_relpath` and
    confirmed non-``None`` by the caller), ``data`` is the decoded file bytes,
    and ``raw_label`` is the original path string used in per-file error
    messages (the multipart part filename or the JSON dict key).

    Returns the materialized ``file_tree`` dict, or a 400 response when a
    per-file (``_SKILL_MAX_FILE_BYTES``) or total (``_SKILL_MAX_TOTAL_BYTES``)
    cap is exceeded — matching the exact error shapes the two callers produced
    before the merge.
    """
    file_tree: dict[str, bytes] = {}
    total = 0
    for rel, data, raw_label in items:
        if len(data) > _SKILL_MAX_FILE_BYTES:
            return web.json_response(
                {
                    "error": "validation",
                    "fields": {"file": [f"{raw_label} exceeds {_SKILL_MAX_FILE_MB}MB"]},
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
    return file_tree


# ── Handler sub-modules ─────────────────────────────────────────────────────
# Imported AFTER the helpers above are defined so the sub-modules can
# ``from bot.webui.routes.pool_config import pool_cfg_required, ...`` without
# hitting a partially-initialised package (circular-import guard).
# noqa: E402 — justified by the ordering constraint above.
from bot.webui.routes.pool_config.mcp import (  # noqa: E402
    handle_delete_mcp,
    handle_read_mcp,
    handle_upsert_mcp,
)
from bot.webui.routes.pool_config.pools import handle_list_pools  # noqa: E402
from bot.webui.routes.pool_config.prompts import (  # noqa: E402
    handle_create_prompt,
    handle_delete_prompt_global,
    handle_list_prompts,
    handle_read_prompt_strict,
    handle_write_prompt_global,
)
from bot.webui.routes.pool_config.skills import (  # noqa: E402
    handle_assign_skill,
    handle_delete_skill,
    handle_list_agent_skills,
    handle_list_skills,
    handle_unassign_skill,
    handle_upload_skill,
    upload_skill_json,
    upload_skill_multipart,
)

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
    app.router.add_get("/api/pools/{pool}/agents/{agent}/skills", handle_list_agent_skills)
    app.router.add_post(
        "/api/pools/{pool}/agents/{agent}/skills/{name}",
        handle_assign_skill,
    )
    app.router.add_delete(
        "/api/pools/{pool}/agents/{agent}/skills/{name}",
        handle_unassign_skill,
    )


__all__ = [
    "_materialize_skill_files",
    "handle_assign_skill",
    "handle_create_prompt",
    "handle_delete_mcp",
    "handle_delete_prompt_global",
    "handle_delete_skill",
    "handle_list_agent_skills",
    "handle_list_pools",
    "handle_list_prompts",
    "handle_list_skills",
    "handle_read_mcp",
    "handle_read_prompt_strict",
    "handle_unassign_skill",
    "handle_upsert_mcp",
    "handle_upload_skill",
    "handle_write_prompt_global",
    "pool_cfg_required",
    "register_pool_config_routes",
    "upload_skill_json",
    "upload_skill_multipart",
]
