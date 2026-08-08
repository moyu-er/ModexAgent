// Graph REST client. Mirrors the backend routes in
// `bot/webui/routes/graph_routes.py`: /api/graphs/specs/* and
// /api/graphs/instances/* (12 endpoints). Per-workspace resolution (G7)
// travels in the `X-Workspace-Id` header; an empty workspace id means the
// home workspace, so the header is omitted (matches the `?ws=` convention).

import { assertOk, API_BASE } from "./api";

const jsonHeaders = { "Content-Type": "application/json" };

// ── Types (mirror bot/webui/routes/graph_models.py) ─────────────────────────

export interface GraphSpecSummary {
  spec_id: string;
  name: string;
  version: string;
}

export interface GraphSpecResponse {
  spec_id: string;
  name: string;
  version: string;
  yaml_content: string;
}

// ── Topology (§11.3) ─────────────────────────────────────────────────────────

export interface NodeTopologyInfo {
  name: string;
  node_type: string;
  config: Record<string, unknown>;
  trigger?: string | null;
}

export interface EdgeTopologyInfo {
  source: string;
  target: string;
}

export interface GraphTopology {
  spec_id: string;
  name: string;
  scheduler: string;
  default_trigger: string;
  nodes: NodeTopologyInfo[];
  edges: EdgeTopologyInfo[];
  entry_node: string;
}

export interface GraphNodeStatus {
  node_name: string;
  node_id: string;
  status: string;
  result?: GraphPayload | null;
}

export interface GraphPayload {
  content: string;
}

export interface GraphInstance {
  spec_id: string;
  graph_instance_id: string;
  status: string;
  nodes: GraphNodeStatus[];
  result: GraphPayload[] | null;
}

export interface GraphRunResponse {
  graph_instance_id: string;
  status: string;
}

export interface GraphEvent {
  kind: string;
  graph_instance_id?: string;
  result?: unknown;
  error?: string | null;
}

export interface GraphControlResponse {
  graph_instance_id: string;
  status: string;
}

/** Instance lifecycle values accepted by the `?status=` filter. */
export const GRAPH_INSTANCE_STATUSES = [
  "pending",
  "running",
  "paused",
  "stopped",
  "crashed",
  "completed",
  "failed",
] as const;

// ── Helpers ─────────────────────────────────────────────────────────────────

function workspaceHeaders(workspaceId: string): Record<string, string> {
  return workspaceId ? { "X-Workspace-Id": workspaceId } : {};
}

// ── Specs ───────────────────────────────────────────────────────────────────

export async function getSpecs(workspaceId: string): Promise<GraphSpecSummary[]> {
  const resp = await fetch(`${API_BASE}/graphs/specs`, {
    headers: workspaceHeaders(workspaceId),
  });
  await assertOk(resp);
  const data = (await resp.json()) as { specs: GraphSpecSummary[] };
  return data.specs;
}

export async function getSpec(
  workspaceId: string,
  specId: string,
): Promise<GraphSpecResponse> {
  const resp = await fetch(`${API_BASE}/graphs/specs/${specId}`, {
    headers: workspaceHeaders(workspaceId),
  });
  await assertOk(resp);
  return resp.json() as Promise<GraphSpecResponse>;
}

export async function updateSpec(
  workspaceId: string,
  specId: string,
  yamlContent: string,
): Promise<GraphSpecResponse> {
  const resp = await fetch(`${API_BASE}/graphs/specs/${specId}`, {
    method: "PUT",
    headers: { ...jsonHeaders, ...workspaceHeaders(workspaceId) },
    body: JSON.stringify({ yaml_content: yamlContent }),
  });
  await assertOk(resp);
  return resp.json() as Promise<GraphSpecResponse>;
}

export async function runGraph(
  workspaceId: string,
  specId: string,
  userInput?: string,
): Promise<GraphRunResponse> {
  const body = userInput ? { user_input: { content: userInput } } : {};
  const resp = await fetch(`${API_BASE}/graphs/specs/${specId}/run`, {
    method: "POST",
    headers: { ...jsonHeaders, ...workspaceHeaders(workspaceId) },
    body: JSON.stringify(body),
  });
  await assertOk(resp);
  return resp.json() as Promise<GraphRunResponse>;
}

/** Raw text/yaml variant of getSpec (endpoint exists; editor uses getSpec). */
export async function getSpecYaml(
  workspaceId: string,
  specId: string,
): Promise<string> {
  const resp = await fetch(`${API_BASE}/graphs/specs/${specId}/yaml`, {
    headers: workspaceHeaders(workspaceId),
  });
  await assertOk(resp);
  return resp.text();
}

/** Structured topology (§11.3) — compiler-validated nodes/edges/scheduler.
 *  Optional optimization; parseGraphSpecYaml remains the fallback. */
export async function getTopology(
  workspaceId: string,
  specId: string,
): Promise<GraphTopology> {
  const resp = await fetch(`${API_BASE}/graphs/specs/${specId}/topology`, {
    headers: workspaceHeaders(workspaceId),
  });
  await assertOk(resp);
  return resp.json() as Promise<GraphTopology>;
}

// ── Instances ───────────────────────────────────────────────────────────────

export async function listInstances(
  workspaceId: string,
  status?: string,
): Promise<GraphInstance[]> {
  const params = status ? `?status=${encodeURIComponent(status)}` : "";
  const resp = await fetch(`${API_BASE}/graphs/instances${params}`, {
    headers: workspaceHeaders(workspaceId),
  });
  await assertOk(resp);
  return resp.json() as Promise<GraphInstance[]>;
}

export async function getInstance(
  workspaceId: string,
  instanceId: string,
): Promise<GraphInstance> {
  const resp = await fetch(`${API_BASE}/graphs/instances/${instanceId}`, {
    headers: workspaceHeaders(workspaceId),
  });
  await assertOk(resp);
  return resp.json() as Promise<GraphInstance>;
}

export async function getEvents(
  workspaceId: string,
  instanceId: string,
): Promise<GraphEvent[]> {
  const resp = await fetch(`${API_BASE}/graphs/instances/${instanceId}/events`, {
    headers: workspaceHeaders(workspaceId),
  });
  await assertOk(resp);
  const data = (await resp.json()) as { events: GraphEvent[] };
  return data.events;
}

async function controlInstance(
  workspaceId: string,
  instanceId: string,
  action: "pause" | "resume" | "stop",
): Promise<GraphControlResponse> {
  const resp = await fetch(`${API_BASE}/graphs/instances/${instanceId}/${action}`, {
    method: "POST",
    headers: workspaceHeaders(workspaceId),
  });
  await assertOk(resp);
  return resp.json() as Promise<GraphControlResponse>;
}

export function pauseGraph(
  workspaceId: string,
  instanceId: string,
): Promise<GraphControlResponse> {
  return controlInstance(workspaceId, instanceId, "pause");
}

export function resumeGraph(
  workspaceId: string,
  instanceId: string,
): Promise<GraphControlResponse> {
  return controlInstance(workspaceId, instanceId, "resume");
}

export function stopGraph(
  workspaceId: string,
  instanceId: string,
): Promise<GraphControlResponse> {
  return controlInstance(workspaceId, instanceId, "stop");
}

export async function deliverToNode(
  workspaceId: string,
  instanceId: string,
  nodeName: string,
  content: string,
): Promise<{ graph_instance_id: string; node_name: string; status: string }> {
  const resp = await fetch(`${API_BASE}/graphs/instances/${instanceId}/deliver`, {
    method: "POST",
    headers: { ...jsonHeaders, ...workspaceHeaders(workspaceId) },
    body: JSON.stringify({ node_name: nodeName, content: { content } }),
  });
  await assertOk(resp);
  return resp.json() as Promise<{
    graph_instance_id: string;
    node_name: string;
    status: string;
  }>;
}
