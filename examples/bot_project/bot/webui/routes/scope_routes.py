"""aiohttp route handlers for the scope declaration REST API (ticket 16).

Thin adapters following the :mod:`bot.webui.routes.graph_routes` convention:
module-level ``async def handle_*`` functions that read server state through
``request.app["server"]`` and resolve per-workspace resources via the
late-bound ``server._graph_workspace_resolver`` (the same seam the graph
routes use — the resolver returns the full ``PoolWorkspaceResources``, whose
``target`` anchors ``config/scopes/bot.yml`` and whose ``ctx`` is the
``WorkspaceContext`` the compiler threads).

Routes registered:
    GET /api/scope/declaration  -- the raw declaration YAML (editor surface).
    PUT /api/scope/declaration  -- validate + atomically write back the YAML
                                   (PoolEditor pattern: writes the file,
                                   restart-effective — N2, no hot reload).
    GET /api/scope/topology     -- the declared scope tree (workspace/pool/
                                   agent levels + peer links) for the canvas.
    GET /api/scope/bill         -- the per-field provenance bill + per-tool
                                   implementation origins + O3 replacement
                                   records (SPEC §3.4 rule 3 / §3.5).

No boot-time cache (SPEC §3.4 data-path ruling): every read reloads the YAML
from disk and recompiles via the pure-function ``compile_scope``, so a WebUI
edit that has not been restarted shows in the bill as the on-disk
declaration (S2 contradiction dissolved).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Final

import yaml
from aiohttp import web
from pydantic import ValidationError

from bot.webui.routes.scope_models import (
    ScopeAgentBill,
    ScopeAgentNode,
    ScopeBillResponse,
    ScopeCapabilityBill,
    ScopeCapabilityContributionBill,
    ScopeDeclarationResponse,
    ScopeDeclarationSaveResponse,
    ScopeDeclarationUpdateRequest,
    ScopeFieldBill,
    ScopeFieldValue,
    ScopeHookBill,
    ScopePoolTopology,
    ScopeReplacementBill,
    ScopeToolBill,
    ScopeTopologyResponse,
)
from modex_agent.plugins.registry import ComponentNotFoundError
from modex_agent.scope import (
    STANDARD_PROFILES,
    AgentSpec,
    CompiledAgent,
    PoolSpec,
    ProvenanceLayer,
    ScopeDeclarationError,
    ScopeKind,
    ScopeSpec,
    ScopeValidationIssue,
    compile_scope,
    load_scope_declaration,
    validate_declaration,
    validate_effective_configs,
)
from modex_agent.scope.compiler import ScopeCompilation

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer
    from bot.workspace.handle import PoolWorkspaceResources

# Use the ``bot.webui.server`` logger so log records from these handlers
# remain attributable to the WebUI server (matches the other route modules).
logger = logging.getLogger("bot.webui.server")

_DECLARATION_RELATIVE_PATH: Final = Path("config") / "scopes" / "bot.yml"
"""The declaration true source, anchored at the workspace target (same
layout convention as ``config/graphs/`` in the graph routes)."""


# ── Helpers ─────────────────────────────────────────────────────────────────


def _resolve_scope_target(
    request: web.Request,
) -> PoolWorkspaceResources | web.Response:
    """Resolve the request's workspace resource bundle.

    Mirrors the graph routes' ``_resolve_resources`` degradation: 503 when
    the workspace resolver is not injected or returns nothing.
    """
    server: WebUIServer = request.app["server"]
    resolver = server._graph_workspace_resolver
    if resolver is None:
        return web.json_response({"error": "workspace resources not configured"}, status=503)
    ws_id = request.headers.get("X-Workspace-Id") or request.query.get("ws", "")
    resources = resolver(ws_id)
    if resources is None:
        return web.json_response({"error": "workspace resources not configured"}, status=503)
    return resources


def _load_declaration(path: Path) -> ScopeSpec | web.Response:
    """Load the on-disk declaration, mapping load failures to responses.

    404 when the file is absent; 409 when the on-disk content is broken
    (the client sent nothing — the disk state precludes serving).
    """
    try:
        return load_scope_declaration(path)
    except FileNotFoundError:
        return web.json_response(
            {"error": "scope declaration not found", "path": str(path)},
            status=404,
        )
    except (yaml.YAMLError, ScopeDeclarationError) as exc:
        return web.json_response({"error": "invalid declaration", "detail": str(exc)}, status=409)
    except ValidationError as exc:
        return web.json_response(
            {"error": "invalid declaration", "detail": exc.errors()}, status=409
        )


def _issues_response(issues: list[ScopeValidationIssue], *, status: int) -> web.Response:
    return web.json_response(
        {
            "error": "declaration invalid",
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
        status=status,
    )


def _compile_declaration(
    spec: ScopeSpec,
    resources: PoolWorkspaceResources,
    *,
    error_status: int,
) -> ScopeCompilation | web.Response:
    try:
        return compile_scope(
            spec,
            workspace_ctx=resources.ctx,
            registry=resources.component_registry,
        )
    except (ComponentNotFoundError, ValueError) as exc:
        return web.json_response(
            {"error": "invalid declaration", "detail": str(exc)},
            status=error_status,
        )


def _declared_pools(spec: ScopeSpec) -> list[PoolSpec]:
    """The declared pools of either root form (V4 guarantees the layer
    matching ``kind`` is set)."""
    if spec.kind is ScopeKind.WORKSPACE and spec.workspace is not None:
        return list(spec.workspace.pools)
    if spec.kind is ScopeKind.POOL and spec.pool is not None:
        return [spec.pool]
    return []


def _find_agent(spec: ScopeSpec, pool: str, agent: str) -> AgentSpec:
    """The declaration node for one compiled agent (compile output covers
    every declared agent in declaration order, so this always resolves)."""
    for pool_spec in _declared_pools(spec):
        if pool_spec.name != pool:
            continue
        for agent_spec in pool_spec.agents:
            if agent_spec.name == agent:
                return agent_spec
    raise KeyError(f"compiled agent {pool}/{agent} missing from declaration")


def _field_value(
    field: str,
    layer: ProvenanceLayer,
    profile: str | None,
    compiled: CompiledAgent,
) -> ScopeFieldValue:
    """The effective value paired with one provenance field entry, pulled
    from the compiled artifacts."""
    match field:
        case "toolset":
            return compiled.defaults.toolset_profile.value
        case "tools":
            return list(compiled.effective.tools)
        case "capabilities":
            # The effective capability set (compile product, registry-
            # enumeration order) — the override map itself stays in the
            # declaration YAML the editor surface serves.
            return [capability.name for capability in compiled.spec.capabilities]
        case "hooks":
            # The final hook roster (position defaults + capability
            # contributions + declared entries, merge order).
            return list(compiled.spec.hooks)
        case "eager":
            return compiled.defaults.registration.value
        case "max_steps":
            return compiled.spec.max_iterations
        case "memory":
            return compiled.spec.memory_overrides.model_dump(mode="json")
    raise KeyError(f"unknown provenance field {field!r}")


def _agent_bill(spec: ScopeSpec, compiled: CompiledAgent) -> ScopeAgentBill:
    prov = compiled.provenance
    agent_spec = _find_agent(spec, prov.pool, prov.agent)
    return ScopeAgentBill(
        pool=prov.pool,
        agent=prov.agent,
        root=agent_spec.parent is None,
        fields=[
            ScopeFieldBill(
                field=fp.field,
                value=_field_value(fp.field, fp.layer, fp.profile, compiled),
                layer=fp.layer,
                profile=fp.profile,
            )
            for fp in prov.fields
        ],
        tools=[
            ScopeToolBill(
                tool=tp.tool,
                origin=tp.origin,
                capability=tp.capability,
                replaces=tp.replaces,
                targets=list(tp.targets),
            )
            for tp in prov.tools
        ],
        hooks=[
            ScopeHookBill(
                hook=hp.hook,
                origin=hp.origin,
                capability=hp.capability,
            )
            for hp in prov.hooks
        ],
        replacements=[
            ScopeReplacementBill(
                default_tool=r.default_tool,
                replacement_tool=r.replacement_tool,
                # Wire field keeps its name until the W4/W5 webui-face
                # migration; the value is the capability registration name.
                supplement=r.capability,
            )
            for r in prov.replacements
        ],
        capabilities=[
            ScopeCapabilityBill(
                capability=capability.capability,
                state=capability.state,
                registration_source=capability.registration_source,
                contributions=[
                    ScopeCapabilityContributionBill(
                        kind=contribution.kind,
                        name=contribution.name,
                        gate=contribution.gate,
                    )
                    for contribution in capability.contributions
                ],
            )
            for capability in prov.capabilities
        ],
    )


# ── Handlers ────────────────────────────────────────────────────────────────


async def handle_get_declaration(request: web.Request) -> web.Response:
    """``GET /api/scope/declaration`` -- the raw declaration YAML text."""
    r = _resolve_scope_target(request)
    if isinstance(r, web.Response):
        return r
    path = Path(r.target) / _DECLARATION_RELATIVE_PATH
    if not path.is_file():
        return web.json_response(
            {"error": "scope declaration not found", "path": str(path)},
            status=404,
        )
    return web.json_response(
        ScopeDeclarationResponse(yaml=path.read_text(encoding="utf-8")).model_dump(mode="json")
    )


async def handle_put_declaration(request: web.Request) -> web.Response:
    """``PUT /api/scope/declaration`` -- validate + atomically write back.

    Follows the graph spec write-back pattern: the new text is staged to a
    sibling temp file, fully validated (structural load, then both validator
    phases — the same gates boot applies), and only then atomically
    committed over the true source. Declaration edits are restart-effective
    (N2): the response always carries ``restart_required: true``.
    """
    r = _resolve_scope_target(request)
    if isinstance(r, web.Response):
        return r
    resources = r
    try:
        body = await request.json()
    except (json.JSONDecodeError, web.HTTPException):
        return web.json_response({"error": "invalid JSON body"}, status=400)
    try:
        update = ScopeDeclarationUpdateRequest.model_validate(body)
    except ValidationError as exc:
        return web.json_response({"error": "validation", "detail": exc.errors()}, status=400)

    path = Path(resources.target) / _DECLARATION_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(update.yaml, encoding="utf-8")
    committed = False
    try:
        try:
            spec = load_scope_declaration(tmp)
        except (yaml.YAMLError, ScopeDeclarationError) as exc:
            return web.json_response(
                {"error": "invalid declaration", "detail": str(exc)}, status=400
            )
        except ValidationError as exc:
            return web.json_response(
                {"error": "invalid declaration", "detail": exc.errors()},
                status=400,
            )
        issues = validate_declaration(spec, profiles=STANDARD_PROFILES.declarations())
        if issues:
            return _issues_response(issues, status=400)
        compilation = _compile_declaration(spec, resources, error_status=400)
        if isinstance(compilation, web.Response):
            return compilation
        effective_issues = validate_effective_configs(
            spec, [c.effective for c in compilation.agents]
        )
        if effective_issues:
            return _issues_response(effective_issues, status=400)
        tmp.replace(path)
        committed = True
    finally:
        if not committed:
            tmp.unlink(missing_ok=True)
    return web.json_response(
        ScopeDeclarationSaveResponse(saved=True, restart_required=True).model_dump(mode="json")
    )


async def handle_get_topology(request: web.Request) -> web.Response:
    """``GET /api/scope/topology`` -- the declared scope tree (structure
    only; validity findings belong to the bill endpoint)."""
    r = _resolve_scope_target(request)
    if isinstance(r, web.Response):
        return r
    spec = _load_declaration(Path(r.target) / _DECLARATION_RELATIVE_PATH)
    if isinstance(spec, web.Response):
        return spec
    return web.json_response(
        ScopeTopologyResponse(
            kind=spec.kind,
            workspace=(spec.workspace.name if spec.workspace is not None else None),
            pools=[
                ScopePoolTopology(
                    name=pool.name,
                    peers=list(pool.peers),
                    agents=[
                        ScopeAgentNode(
                            name=agent.name,
                            parent=agent.parent,
                            root=agent.parent is None,
                        )
                        for agent in pool.agents
                    ],
                )
                for pool in _declared_pools(spec)
            ],
        ).model_dump(mode="json")
    )


async def handle_get_bill(request: web.Request) -> web.Response:
    """``GET /api/scope/bill`` -- the provenance bill, recomputed from the
    on-disk YAML on every request (no boot-time cache, SPEC §3.4)."""
    r = _resolve_scope_target(request)
    if isinstance(r, web.Response):
        return r
    resources = r
    spec = _load_declaration(Path(resources.target) / _DECLARATION_RELATIVE_PATH)
    if isinstance(spec, web.Response):
        return spec
    issues = validate_declaration(spec, profiles=STANDARD_PROFILES.declarations())
    if issues:
        return _issues_response(issues, status=409)
    compilation = _compile_declaration(spec, resources, error_status=409)
    if isinstance(compilation, web.Response):
        return compilation
    return web.json_response(
        ScopeBillResponse(agents=[_agent_bill(spec, c) for c in compilation.agents]).model_dump(
            mode="json"
        )
    )


# ── Registration ────────────────────────────────────────────────────────────


def register_scope_routes(server: WebUIServer) -> None:
    """Register the scope declaration routes on ``server.app.router``.

    ``app["server"]`` is set by :func:`bot.webui.routes.models.register_models_routes`
    (called earlier from :meth:`WebUIServer._setup_routes`); assert
    defensively so a future reordering surfaces a clear error rather than a
    KeyError inside a request handler.

    Called from :meth:`WebUIServer._setup_routes`.
    """
    app = server.app
    if "server" not in app:
        app["server"] = server
    app.router.add_get("/api/scope/declaration", handle_get_declaration)
    app.router.add_put("/api/scope/declaration", handle_put_declaration)
    app.router.add_get("/api/scope/topology", handle_get_topology)
    app.router.add_get("/api/scope/bill", handle_get_bill)


__all__ = ["register_scope_routes"]
