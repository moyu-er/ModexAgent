// Scope declaration REST client (ticket 16). Mirrors the backend routes in
// `bot/webui/routes/scope_routes.py`: /api/scope/declaration (GET/PUT),
// /api/scope/topology, /api/scope/bill. Settings views are home-workspace
// scoped (same as the pools API), so no X-Workspace-Id header is sent; the
// bill recomputes from the on-disk YAML per request (no cache, SPEC §3.4).

import { assertOk, API_BASE } from "./api";

// ── Types (mirror bot/webui/routes/scope_models.py) ─────────────────────────

export interface ScopeAgentNode {
  name: string;
  parent: string | null;
  root: boolean;
}

export interface ScopePoolTopology {
  name: string;
  peers: string[];
  agents: ScopeAgentNode[];
}

export interface ScopeTopology {
  kind: "workspace" | "pool";
  /** Workspace name; null for a pool-as-root declaration. */
  workspace: string | null;
  pools: ScopePoolTopology[];
}

export type ScopeFieldValue =
  | string
  | number
  | string[]
  | Record<string, number | boolean | null>;

export interface ScopeFieldBill {
  field: string;
  value: ScopeFieldValue;
  /** Winning source layer: framework | profile | local (SPEC §3.4). */
  layer: string;
  profile: string | null;
}

export interface ScopeToolBill {
  tool: string;
  /** Implementation origin: preset | profile_tools | local_tools |
   *  supplement | derived_task | derived_send_to_agent | derived_send_to_peer. */
  origin: string;
  /** O3: the default entry this supplement entry replaced (e.g. "edit"). */
  replaces: string | null;
  targets: string[];
}

export interface ScopeReplacementBill {
  default_tool: string;
  replacement_tool: string;
  supplement: string;
}

export interface ScopeAgentBill {
  pool: string;
  agent: string;
  root: boolean;
  fields: ScopeFieldBill[];
  tools: ScopeToolBill[];
  replacements: ScopeReplacementBill[];
}

export interface ScopeDeclarationSave {
  saved: boolean;
  restart_required: boolean;
}

// ── Endpoints ───────────────────────────────────────────────────────────────

export async function getScopeDeclaration(): Promise<string> {
  const resp = await fetch(`${API_BASE}/scope/declaration`);
  await assertOk(resp);
  const data = (await resp.json()) as { yaml: string };
  return data.yaml;
}

export async function saveScopeDeclaration(
  yaml: string,
): Promise<ScopeDeclarationSave> {
  const resp = await fetch(`${API_BASE}/scope/declaration`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml }),
  });
  await assertOk(resp);
  return resp.json() as Promise<ScopeDeclarationSave>;
}

export async function getScopeTopology(): Promise<ScopeTopology> {
  const resp = await fetch(`${API_BASE}/scope/topology`);
  await assertOk(resp);
  return resp.json() as Promise<ScopeTopology>;
}

export async function getScopeBill(): Promise<ScopeAgentBill[]> {
  const resp = await fetch(`${API_BASE}/scope/bill`);
  await assertOk(resp);
  const data = (await resp.json()) as { agents: ScopeAgentBill[] };
  return data.agents;
}
