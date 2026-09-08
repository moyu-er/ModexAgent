"""aiohttp route handlers for the models/config/restart REST API.

Thin adapters extracted from :class:`bot.webui.server.WebUIServer`. Each
handler is a module-level async function that reads server state through
``request.app["server"]`` (set by :func:`register_models_routes`), matching
the ``control_facade`` pattern in :mod:`bot.control.routes`.

Routes registered:
    GET   /api/models              -- list (provider, model, default) choices.
    GET   /api/config/{domain}     -- masked config payload.
    PUT   /api/config/{domain}     -- validate + persist config.
    POST  /api/system/restart      -- schedule a process restart.
    POST  /api/models/fetch        -- fetch a provider's model list server-side.

The cleanup callback :func:`close_http_session` is appended to
``app.on_cleanup`` so the lazy-shared :class:`aiohttp.ClientSession` is closed
on shutdown.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiohttp import ClientSession, ClientTimeout, web
from pydantic import ValidationError

from bot.service.config_controller import FieldValidationError
from bot.service.model_config import ProviderCfg
from bot.webui.model_fetch import FetchModelsReq, ModelFetchError
from modex_agent.ioc.configs.llm import InterfaceFormat

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (preserves log-capture tests that
# filter by ``bot.webui.server``).
logger = logging.getLogger("bot.webui.server")


# ── Handlers ────────────────────────────────────────────────────────────────


async def handle_models(request: web.Request) -> web.Response:
    """``GET /api/models`` -- list (provider_name, model_name, default) choices
    for the frontend model selector.

    Re-reads model.yml live so CLI edits appear without a restart. Only
    provider_name / model_name / default are returned -- NEVER api_key or url
    (those stay server-side).
    """
    server: WebUIServer = request.app["server"]
    loader = server._model_config_loader
    cfg = loader() if loader is not None else None
    if cfg is None:
        return web.json_response({"choices": []})
    default = (cfg.default_provider, cfg.default_model)
    choices = [
        {"provider_name": p, "model_name": m, "default": (p, m) == default}
        for (p, m) in cfg.all_choices()
    ]
    return web.json_response({"choices": choices})


async def handle_get_config(request: web.Request) -> web.Response:
    """``GET /api/config/{domain}`` -- masked config payload."""
    server: WebUIServer = request.app["server"]
    if server._config_controller is None:
        return web.json_response({"error": "config not configured"}, status=503)
    domain = request.match_info["domain"]
    try:
        payload = server._config_controller.read(domain)
    except KeyError:
        return web.json_response({"error": f"unknown domain: {domain}"}, status=404)
    except Exception as exc:  # noqa: BLE001 - malformed YAML / IO errors surface readably
        logger.exception("config read failed for domain %s", domain)
        return web.json_response({"error": f"config read failed: {exc}"}, status=500)
    return web.json_response(payload.model_dump(mode="json"))


async def handle_put_config(request: web.Request) -> web.Response:
    """``PUT /api/config/{domain}`` -- validate + persist. Never auto-applies."""
    server: WebUIServer = request.app["server"]
    if server._config_controller is None:
        return web.json_response({"error": "config not configured"}, status=503)
    domain = request.match_info["domain"]
    try:
        body = await request.json()
    except Exception as exc:  # noqa: BLE001 - malformed JSON body
        logger.warning("Failed to parse config JSON body: %s", exc)
        return web.json_response({"error": "invalid body"}, status=400)
    try:
        payload = server._config_controller.write(domain, body)
    except KeyError:
        return web.json_response({"error": f"unknown domain: {domain}"}, status=404)
    except FieldValidationError as exc:
        return web.json_response({"error": "validation", "fields": exc.errors}, status=400)
    except Exception:  # noqa: BLE001 - unexpected write failure
        logger.exception("config write failed for domain %s", domain)
        return web.json_response({"error": "write failed"}, status=500)
    return web.json_response(payload.model_dump(mode="json"))


async def handle_restart(request: web.Request) -> web.Response:
    """``POST /api/system/restart`` -- schedule a process restart."""
    server: WebUIServer = request.app["server"]
    if server._config_controller is None:
        return web.json_response({"error": "config not configured"}, status=503)
    try:
        server._config_controller.restart()
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


async def handle_fetch_provider_models(request: web.Request) -> web.Response:
    """``POST /api/models/fetch`` -- fetch a provider's model list server-side.

    Unified schema (:class:`FetchModelsReq`): ``{provider_key?, base_url?,
    api_key?, interface_format?, models_url?}``. Inline fields take
    priority; missing fields fall back to the saved provider looked up
    by ``provider_key`` in ``model.yml``. After merge, ``api_key`` and
    (``base_url`` or ``models_url``) must be non-empty.

    ``api_key`` is never logged; only ``provider_key`` and/or
    ``base_url`` appear in diagnostics.
    """
    server: WebUIServer = request.app["server"]
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
    if req.provider_key and server._model_config_loader is not None:
        cfg = server._model_config_loader()
        if cfg is not None:
            saved = cfg.find_provider_by_key(req.provider_key)
        # If saved is None (model.yml missing, unparseable, or key not
        # yet saved), fall through to inline values below instead of
        # erroring -- the user may be fetching models for a brand-new
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

    session = await get_http_session(server)
    # Deferred import so test patches on ``bot.webui.server.fetch_provider_models``
    # are visible to this handler. Importing at module load would bind the
    # original function and bypass the patch.
    from bot.webui.server import fetch_provider_models

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


# ── HTTP session helpers ────────────────────────────────────────────────────


async def get_http_session(server: WebUIServer) -> ClientSession:
    """Return the lazy-shared :class:`aiohttp.ClientSession` on *server*.

    Created on first use; subsequent calls reuse it. The session is closed
    on app shutdown by :func:`close_http_session`.
    """
    session = getattr(server, "_http_session", None)
    if session is None or session.closed:
        session = ClientSession(timeout=ClientTimeout(total=15))
        server._http_session = session
    return session


async def close_http_session(app: web.Application) -> None:
    """``app.on_cleanup`` callback -- close the shared ClientSession."""
    server: WebUIServer | None = app.get("server")
    if server is None:
        return
    session = getattr(server, "_http_session", None)
    if session is not None and not session.closed:
        await session.close()
        server._http_session = None


# ── Registration ────────────────────────────────────────────────────────────


def register_models_routes(server: WebUIServer) -> None:
    """Register the models/config/restart routes on ``server.app.router``.

    Stores ``server`` on ``app["server"]`` so the route handlers can reach
    server state (config controller, model loader, http session). Also
    appends :func:`close_http_session` to ``app.on_cleanup``.

    Called from :meth:`WebUIServer._setup_routes`.
    """
    app = server.app
    app["server"] = server  # make server accessible to route handlers
    app.router.add_get("/api/models", handle_models)
    app.router.add_get("/api/config/{domain}", handle_get_config)
    app.router.add_put("/api/config/{domain}", handle_put_config)
    app.router.add_post("/api/system/restart", handle_restart)
    app.router.add_post("/api/models/fetch", handle_fetch_provider_models)
    app.on_cleanup.append(close_http_session)


__all__ = [
    "close_http_session",
    "get_http_session",
    "handle_fetch_provider_models",
    "handle_get_config",
    "handle_models",
    "handle_put_config",
    "handle_restart",
    "register_models_routes",
]
