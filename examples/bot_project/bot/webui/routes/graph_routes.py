"""Graph REST API (13 endpoints). per-workspace resolution via header/query.
spec store returns records with id+metadata for spec responses. PUT validates before save.
Topology endpoint (§11.3) returns compiler-validated structured topology. Node result (§11.4)
extracts completed node output from the persisted graph I/O record with truncation.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from aiohttp import web
from pydantic import ValidationError

from bot.webui.routes.graph_models import (
    EdgeTopologyInfo,
    GraphDeliverRequest,
    GraphEventItem,
    GraphEventListResponse,
    GraphInstanceResponse,
    GraphInvocationResponse,
    GraphRunRecordResponse,
    GraphRunRequest,
    GraphRunResponse,
    GraphSpecListResponse,
    GraphSpecResponse,
    GraphSpecSummary,
    GraphSpecUpdateRequest,
    GraphTopologyResponse,
    NodeStatusInfo,
    NodeTopologyInfo,
)
from modex_agent.agents.agent_node import AgentNode
from modex_agent.orchestration import GraphOrchestrator
from modex_graph import (
    GraphInstanceStatus,
    GraphNode,
    GraphOutput,
    GraphPayload,
    GraphSpec,
    TopologyError,
)

if TYPE_CHECKING:
    from bot.webui.server import WebUIServer
    from bot.workspace.handle import PoolWorkspaceResources


def _resolve_resources(
    request: web.Request,
) -> tuple[GraphOrchestrator, dict[int, list[GraphOutput]] | None, PoolWorkspaceResources] | web.Response:
    server: WebUIServer = request.app["server"]
    resolver = server._graph_workspace_resolver
    if resolver is None:
        return web.json_response({"error": "graph workspace manager not configured"}, status=503)
    ws_id = request.headers.get("X-Workspace-Id") or request.query.get("ws", "")
    resources = resolver(ws_id)
    if resources is None or resources.graph_orchestrator is None:
        return web.json_response({"error": "graph orchestrator not configured"}, status=503)
    return resources.graph_orchestrator, resources.graph_event_store, resources


def _int_param(request: web.Request, name: str) -> int | web.Response:
    raw = request.match_info.get(name, "")
    try:
        return int(raw)
    except ValueError:
        return web.json_response({"error": f"invalid {name}: {raw!r}"}, status=400)


def _yaml(spec: GraphSpec) -> str:
    """Serialize a GraphSpec to YAML for API responses (GET spec / PUT spec).

    KNOWN CONTRACT DEBT — deferred convergence (tracked in scope-converge
    HANDOFF.md §4 Leftovers #5): ``model_dump()`` emits unset optional
    fields as ``null`` (``state_schema: null``, per-node ``trigger: null``),
    so API YAML is noisier than the hand-written files in ``config/graphs/``
    and the noise propagates into saved files via editor round-trips
    (PUT writes the editor text verbatim). The WebUI YAML parser tolerates
    these keys (graph-IA fix, 2026-08-20), so this is hygiene, not a bug.

    Planned fix: ``model_dump(mode="json", exclude_none=True)`` — verified
    round-trip lossless; free-form ``config`` dict nulls preserved (only
    model fields are dropped); declarative ``state_schema`` blocks fully
    serialized. Store idempotency is unaffected: ``save_if_changed``
    compares ``model_dump_json()``, never this output.
    """
    return yaml.dump(spec.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


async def _json_body(request: web.Request) -> dict[str, Any] | web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, web.HTTPException):
        return web.json_response({"error": "invalid JSON body"}, status=400)
    return (
        body
        if isinstance(body, dict)
        else web.json_response({"error": "JSON body must be an object"}, status=400)
    )


async def handle_list_specs(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    records = orch._spec_store.list_records()
    return web.json_response(
        GraphSpecListResponse(
            specs=[
                GraphSpecSummary(spec_id=str(rec.spec_id), name=rec.name, version=rec.version)
                for rec in records
            ]
        ).model_dump(mode="json")
    )


async def handle_get_spec(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    sid = _int_param(request, "spec_id")
    if isinstance(sid, web.Response):
        return sid
    store = orch._spec_store
    record = store.get_by_id(sid)
    if record is None:
        return web.json_response({"error": f"spec {sid} not found"}, status=404)
    spec = store.load_by_id(sid)
    if spec is None:
        return web.json_response({"error": "spec content missing"}, status=500)
    return web.json_response(
        GraphSpecResponse(
            spec_id=str(record.spec_id),
            name=record.name,
            version=record.version,
            yaml_content=_yaml(spec),
        ).model_dump(mode="json")
    )


async def handle_put_spec(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, resources = r
    sid = _int_param(request, "spec_id")
    if isinstance(sid, web.Response):
        return sid
    store = orch._spec_store
    record = store.get_by_id(sid)
    if record is None:
        return web.json_response({"error": f"spec {sid} not found"}, status=404)
    body = await _json_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        update_req = GraphSpecUpdateRequest.model_validate(body)
    except ValidationError as exc:
        return web.json_response({"error": "validation", "detail": exc.errors()}, status=400)
    try:
        spec_dict = yaml.safe_load(update_req.yaml_content)
        if not isinstance(spec_dict, dict):
            return web.json_response({"error": "YAML root must be a mapping"}, status=400)
        spec = GraphSpec.model_validate(spec_dict)
        orch._compiler.validate(spec)
    except (yaml.YAMLError, ValidationError) as exc:
        return web.json_response(
            {
                "error": "invalid spec",
                "detail": exc.errors() if isinstance(exc, ValidationError) else str(exc),
            },
            status=400,
        )
    except TopologyError as exc:
        return web.json_response(
            {"error": "topology validation failed", "detail": str(exc)}, status=400
        )
    new_spec_id = store.save_if_changed(spec)
    graphs_dir = Path(resources.target) / "config" / "graphs"
    graphs_dir.mkdir(parents=True, exist_ok=True)
    (graphs_dir / f"{spec.name}.yml").write_text(update_req.yaml_content, encoding="utf-8")
    saved_spec = store.load_by_id(new_spec_id)
    if saved_spec is None:
        return web.json_response({"error": "save succeeded but load failed"}, status=500)
    return web.json_response(
        GraphSpecResponse(
            spec_id=str(new_spec_id),
            name=saved_spec.name,
            version=saved_spec.version,
            yaml_content=_yaml(saved_spec),
        ).model_dump(mode="json")
    )


async def handle_run_spec(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    sid = _int_param(request, "spec_id")
    if isinstance(sid, web.Response):
        return sid
    try:
        body = await request.json()
    except (json.JSONDecodeError, web.HTTPException):
        body = {}
    run_req = GraphRunRequest.model_validate(body if isinstance(body, dict) else {})
    try:
        gid = await orch.create_instance(sid, user_input=run_req.user_input)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    orch.start_run(gid)
    return web.json_response(
        GraphRunResponse(
            graph_instance_id=str(gid), status=GraphInstanceStatus.PENDING.value
        ).model_dump(mode="json")
    )


async def handle_run_instance(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    gid = _int_param(request, "instance_id")
    if isinstance(gid, web.Response):
        return gid
    try:
        body = await request.json()
    except (json.JSONDecodeError, web.HTTPException):
        body = {}
    run_req = GraphRunRequest.model_validate(body if isinstance(body, dict) else {})
    metadata = orch._instance_store.load(gid)
    if metadata is None:
        return web.json_response({"error": f"instance {gid} not found"}, status=404)
    try:
        orch.start_run(gid, user_input=run_req.user_input)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    return web.json_response(
        GraphRunResponse(
            graph_instance_id=str(gid), status=GraphInstanceStatus.PENDING.value
        ).model_dump(mode="json")
    )


async def handle_invoke_instance(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    gid = _int_param(request, "instance_id")
    if isinstance(gid, web.Response):
        return gid
    try:
        body = await request.json()
    except (json.JSONDecodeError, web.HTTPException):
        body = {}
    run_req = GraphRunRequest.model_validate(body if isinstance(body, dict) else {})
    if orch._instance_store.load(gid) is None:
        return web.json_response({"error": f"instance {gid} not found"}, status=404)
    try:
        orch.start_invoke(gid, user_input=run_req.user_input)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response(
        GraphRunResponse(
            graph_instance_id=str(gid), status=GraphInstanceStatus.RUNNING.value
        ).model_dump(mode="json")
    )


async def handle_get_spec_yaml(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    sid = _int_param(request, "spec_id")
    if isinstance(sid, web.Response):
        return sid
    spec = orch._spec_store.load_by_id(sid)
    if spec is None:
        return web.json_response({"error": f"spec {sid} not found"}, status=404)
    return web.Response(text=_yaml(spec), content_type="text/yaml")


async def handle_get_topology(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    sid = _int_param(request, "spec_id")
    if isinstance(sid, web.Response):
        return sid
    store = orch._spec_store
    record = store.get_by_id(sid)
    if record is None:
        return web.json_response({"error": f"spec {sid} not found"}, status=404)
    spec = store.load_by_id(sid)
    if spec is None:
        return web.json_response({"error": "spec content missing"}, status=500)
    try:
        orch._compiler.validate(spec)
    except TopologyError as exc:
        return web.json_response(
            {"error": "topology validation failed", "detail": str(exc)}, status=400
        )
    return web.json_response(
        GraphTopologyResponse(
            spec_id=str(sid),
            name=spec.name,
            scheduler=spec.scheduler.value,
            default_trigger=spec.default_trigger.value,
            nodes=[
                NodeTopologyInfo(
                    name=n.name,
                    node_type=n.node_type,
                    config=dict(n.config),
                    trigger=n.trigger.value if n.trigger is not None else None,
                )
                for n in spec.nodes
            ],
            edges=[
                EdgeTopologyInfo(source=e.source, target=e.target) for e in spec.edges
            ],
            entry_node="__start__",
        ).model_dump(mode="json")
    )


async def _control_instance(
    request: web.Request,
    fn: Callable[[GraphOrchestrator, int], Awaitable[None]],
) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    gid = _int_param(request, "instance_id")
    if isinstance(gid, web.Response):
        return gid
    if orch._instance_store.load(gid) is None:
        return web.json_response({"error": f"instance {gid} not found"}, status=404)
    try:
        await fn(orch, gid)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    return web.json_response(
        GraphRunResponse(
            graph_instance_id=str(gid), status=orch.get_state(gid).metadata.status.value,
        ).model_dump(mode="json")
    )


async def handle_pause_instance(request: web.Request) -> web.Response:
    return await _control_instance(request, GraphOrchestrator.pause)


async def handle_resume_instance(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    gid = _int_param(request, "instance_id")
    if isinstance(gid, web.Response):
        return gid
    metadata = orch._instance_store.load(gid)
    if metadata is None:
        return web.json_response({"error": f"instance {gid} not found"}, status=404)
    if metadata.status != GraphInstanceStatus.PAUSED:
        return web.json_response(
            {
                "error": f"Cannot resume instance {gid}: status is {metadata.status.value}, must be PAUSED"
            },
            status=400,
        )
    try:
        orch.start_resume(gid)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    return web.json_response({"graph_instance_id": str(gid), "status": GraphInstanceStatus.RUNNING.value})


async def handle_stop_instance(request: web.Request) -> web.Response:
    return await _control_instance(request, GraphOrchestrator.stop)


async def handle_deliver_to_node(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    gid = _int_param(request, "instance_id")
    if isinstance(gid, web.Response):
        return gid
    body = await _json_body(request)
    if isinstance(body, web.Response):
        return body
    try:
        deliver_req = GraphDeliverRequest.model_validate(body)
    except ValidationError as exc:
        return web.json_response({"error": "validation", "detail": exc.errors()}, status=400)
    try:
        await orch.deliver_to_node(gid, deliver_req.node_name, deliver_req.content)
    except ValueError as exc:
        return web.json_response(
            {"ok": False, "target": deliver_req.node_name, "message": str(exc)},
            status=404,
        )
    return web.json_response(
        {
            "ok": True,
            "target": deliver_req.node_name,
            "message": f"Delivered to {deliver_req.node_name!r}.",
            "graph_instance_id": str(gid),
        }
    )


_RESULT_MAX_CHARS = 500


def _extract_node_result(output: list[GraphPayload] | None) -> GraphPayload | None:
    """Extract a completed END node's summary from the graph invocation output.

    The persisted output is ``list[GraphPayload]``. We take the first payload
    and truncate ``content`` to ``_RESULT_MAX_CHARS``. Returns ``None`` when no
    result is available.
    """
    if not output:
        return None
    content = output[0].content
    if len(content) > _RESULT_MAX_CHARS:
        content = content[:_RESULT_MAX_CHARS] + "..."
    return GraphPayload(content=content)


async def handle_get_instance(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    gid = _int_param(request, "instance_id")
    if isinstance(gid, web.Response):
        return gid
    try:
        snapshot = orch.get_state(gid)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=404)
    id_to_name = {nid: name for name, nid in snapshot.metadata.node_id_map.items()}
    io_record = orch._io_store.get_latest_by_instance(gid)
    instance_output = io_record.output if io_record is not None else None
    end_nid = snapshot.metadata.node_id_map.get(GraphNode.END)
    session_id_map: dict[str, str] = {}
    instance = orch._active_instances.get(gid)
    if instance is not None and instance.compiled is not None:
        for node in instance.compiled.nodes.values():
            if isinstance(node, AgentNode) and node._session is not None:
                session_id_map[node.node_id] = node._session.session_id
    nodes = [
        NodeStatusInfo(
            node_name=id_to_name.get(nid, nid),
            node_id=nid,
            status=inv[-1].status.value if inv else "unknown",
            result=_extract_node_result(instance_output)
            if nid == end_nid and inv and inv[-1].status.value == "completed"
            else None,
            session_id=session_id_map.get(nid),
        )
        for nid, inv in snapshot.nodes.items()
    ]
    end_invocations = snapshot.nodes.get(end_nid, []) if end_nid is not None else []
    # Node-level END result is a 500-character preview; end_result intentionally keeps full output.
    end_result = (
        instance_output
        if instance_output
        and end_invocations
        and end_invocations[-1].status.value == "completed"
        else None
    )
    return web.json_response(
        GraphInstanceResponse(
            spec_id=str(snapshot.metadata.spec_id),
            graph_instance_id=str(snapshot.metadata.graph_instance_id),
            status=snapshot.metadata.status.value,
            nodes=nodes,
            result=end_result,
            created_at=snapshot.metadata.created_at,
            updated_at=snapshot.metadata.updated_at,
        ).model_dump(mode="json")
    )


async def handle_list_instances(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    store = orch._instance_store
    sf = request.query.get("status")
    try:
        metadatas = (
            store.load_by_status(GraphInstanceStatus(sf))
            if sf
            else [m for st in GraphInstanceStatus for m in store.load_by_status(st)]
        )
    except ValueError:
        return web.json_response({"error": f"invalid status: {sf!r}"}, status=400)
    spec_id_raw = request.query.get("spec_id")
    if spec_id_raw is not None:
        try:
            spec_id = int(spec_id_raw)
        except ValueError:
            return web.json_response({"error": f"invalid spec_id: {spec_id_raw!r}"}, status=400)
        metadatas = [m for m in metadatas if m.spec_id == spec_id]
    return web.json_response(
        [
            GraphInstanceResponse(
                spec_id=str(m.spec_id),
                graph_instance_id=str(m.graph_instance_id),
                status=m.status.value,
                nodes=[],
                created_at=m.created_at,
                updated_at=m.updated_at,
            ).model_dump(mode="json")
            for m in metadatas
        ]
    )


async def handle_list_runs(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    sid = _int_param(request, "spec_id")
    if isinstance(sid, web.Response):
        return sid
    records = orch._io_store.list_by_spec(sid)
    instance_store = orch._instance_store
    runs: list[GraphRunRecordResponse] = []
    for record in records:
        metadata = instance_store.load(record.graph_instance_id)
        runs.append(
            GraphRunRecordResponse(
                record_id=str(record.record_id),
                graph_instance_id=str(record.graph_instance_id),
                user_input=record.user_input,
                output=record.output,
                status=metadata.status.value if metadata is not None else "unknown",
                created_at=record.created_at,
                updated_at=metadata.updated_at if metadata is not None else 0,
            )
        )
    return web.json_response([run.model_dump(mode="json") for run in runs])


async def handle_list_invocations(request: web.Request) -> web.Response:
    """List all I/O records (invocations) for a graph instance, ordered by version.

    Returns the ``conversation history`` of a graph instance — each invocation's
    user_input and output. Mirrors ``handle_list_runs`` but scoped by instance_id
    via ``orch._io_store.list_by_instance(gid)`` (ADR-0040).
    """
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    orch, _, _ = r
    gid = _int_param(request, "instance_id")
    if isinstance(gid, web.Response):
        return gid
    records = orch._io_store.list_by_instance(gid)
    invocations: list[GraphInvocationResponse] = [
        GraphInvocationResponse(
            record_id=str(rec.record_id),
            version=rec.version,
            user_input=rec.user_input,
            output=rec.output,
            created_at=rec.created_at,
        )
        for rec in records
    ]
    return web.json_response([inv.model_dump(mode="json") for inv in invocations])


async def handle_get_events(request: web.Request) -> web.Response:
    r = _resolve_resources(request)
    if isinstance(r, web.Response):
        return r
    _, event_store, _ = r
    gid = _int_param(request, "instance_id")
    if isinstance(gid, web.Response):
        return gid
    raw_events = (
        [o.model_dump(mode="json") for o in event_store.get(gid, [])] if event_store else []
    )
    events = [
        GraphEventItem(
            graph_instance_id=str(ev.get("graph_instance_id", "")),
            kind=str(ev.get("kind", "")),
            **{k: v for k, v in ev.items() if k not in ("graph_instance_id", "kind")},
        )
        for ev in raw_events
    ]
    return web.json_response(GraphEventListResponse(events=events).model_dump(mode="json"))


def register_graph_routes(
    server: WebUIServer,
    workspace_manager: Callable[[str], PoolWorkspaceResources | None] | None,
) -> None:
    app = server.app
    if "server" not in app:
        app["server"] = server
    server._graph_workspace_resolver = workspace_manager
    app.router.add_get("/api/graphs/specs", handle_list_specs)
    app.router.add_get("/api/graphs/specs/{spec_id}", handle_get_spec)
    app.router.add_put("/api/graphs/specs/{spec_id}", handle_put_spec)
    app.router.add_post("/api/graphs/specs/{spec_id}/run", handle_run_spec)
    app.router.add_get("/api/graphs/specs/{spec_id}/yaml", handle_get_spec_yaml)
    app.router.add_get("/api/graphs/specs/{spec_id}/topology", handle_get_topology)
    app.router.add_get("/api/graphs/specs/{spec_id}/runs", handle_list_runs)
    app.router.add_get("/api/graphs/instances", handle_list_instances)
    app.router.add_get("/api/graphs/instances/{instance_id}", handle_get_instance)
    app.router.add_get("/api/graphs/instances/{instance_id}/events", handle_get_events)
    app.router.add_get("/api/graphs/instances/{instance_id}/invocations", handle_list_invocations)
    app.router.add_post("/api/graphs/instances/{instance_id}/run", handle_run_instance)
    app.router.add_post("/api/graphs/instances/{instance_id}/invoke", handle_invoke_instance)
    app.router.add_post("/api/graphs/instances/{instance_id}/pause", handle_pause_instance)
    app.router.add_post("/api/graphs/instances/{instance_id}/resume", handle_resume_instance)
    app.router.add_post("/api/graphs/instances/{instance_id}/stop", handle_stop_instance)
    app.router.add_post("/api/graphs/instances/{instance_id}/deliver", handle_deliver_to_node)


__all__ = ["register_graph_routes"]
