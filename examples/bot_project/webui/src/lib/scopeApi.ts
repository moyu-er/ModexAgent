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
  skills_eligible: boolean;
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
   *  supplement | capability_derived | derived_task | derived_send_to_agent |
   *  derived_send_to_peer. */
  origin: string;
  /** Set when capability_derived — the carrying capability's name. */
  capability: string | null;
  /** O3: the default entry this supplement entry replaced (e.g. "edit"). */
  replaces: string | null;
  targets: string[];
}

export interface ScopeReplacementBill {
  default_tool: string;
  replacement_tool: string;
  supplement: string;
}

export interface ScopeHookBill {
  hook: string;
  /** Origin: position_default | capability_derived | local_hooks. */
  origin: string;
  /** Set when capability_derived — the carrying capability's name. */
  capability: string | null;
}

export interface ScopeCapabilityContributionBill {
  /** Component category: tool | hook | section. */
  kind: string;
  name: string;
  /** Gate result: vouched | dropped. */
  gate: string;
}

export interface ScopeCapabilityBill {
  capability: string;
  /** Enablement outcome: auto | declared | vetoed. */
  state: "auto" | "declared" | "vetoed";
  registration_source: string | null;
  contributions: ScopeCapabilityContributionBill[];
}

export interface ScopeAgentBill {
  pool: string;
  agent: string;
  root: boolean;
  fields: ScopeFieldBill[];
  tools: ScopeToolBill[];
  hooks: ScopeHookBill[];
  replacements: ScopeReplacementBill[];
  capabilities: ScopeCapabilityBill[];
}

export interface ScopeDeclarationSave {
  saved: boolean;
  restart_required: boolean;
}

// ── Structured model (pools config panel) ─────────────────────────────────

/**
 * The declaration as a JSON tree (verbatim `yaml.safe_load` of the file).
 * The shape is owned by the backend loader/validator chain, so the client
 * types it as an open mapping; the pools panel's `scopeModel.ts` module
 * holds the typed accessors over it.
 */
export type ScopeModelTree = Record<string, unknown>;

/** One validation finding from a rejected PUT (rule-numbered, node-named). */
export interface ScopeModelIssue {
  rule: string;
  node: string;
  message: string;
}

export interface ScopePositionDefaultRow {
  toolset: string;
  registration: string;
}

/** What one capability carries (tools + hooks) — the bundle unit (ADR-0047). */
export interface ScopeCapabilityBundle {
  tools: string[];
  hooks: string[];
}

/** Enumeration source for every pools-panel form control (hardcode nothing). */
export interface ScopeOptions {
  toolsets: string[];
  context_modes: string[];
  execution_strategies: string[];
  provider_kinds: string[];
  capabilities: string[];
  capability_bundles: Record<string, ScopeCapabilityBundle>;
  hooks: string[];
  default_hooks: string[];
  interceptors: string[];
  commands: string[];
  mcp_servers: string[];
  position_defaults: {
    root: ScopePositionDefaultRow;
    sub: ScopePositionDefaultRow;
  };
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

export async function getScopeModel(): Promise<ScopeModelTree> {
  const resp = await fetch(`${API_BASE}/scope/model`);
  await assertOk(resp);
  const data = (await resp.json()) as { model: ScopeModelTree };
  return data.model;
}

/**
 * PUT the whole declaration tree. On success the backend canonicalizes the
 * file (deviations only), so callers must re-fetch GET /api/scope/model and
 * reset their form state from what comes back. On 400 the ApiError detail
 * is a JSON body with `issues: ScopeModelIssue[]`.
 */
export async function saveScopeModel(
  model: ScopeModelTree,
): Promise<ScopeDeclarationSave> {
  const resp = await fetch(`${API_BASE}/scope/model`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  await assertOk(resp);
  return resp.json() as Promise<ScopeDeclarationSave>;
}

export async function getScopeOptions(): Promise<ScopeOptions> {
  const resp = await fetch(`${API_BASE}/scope/options`);
  await assertOk(resp);
  return resp.json() as Promise<ScopeOptions>;
}

/**
 * POST the draft tree to /api/scope/preview — the PUT's gate chain (load →
 * validate → compile → validate-effective) WITHOUT the commit. Returns the
 * bill-shaped effective view of the draft; on 400 the ApiError detail is a
 * JSON body with `issues: ScopeModelIssue[]`.
 */
export async function previewScopeModel(
  model: ScopeModelTree,
): Promise<ScopeAgentBill[]> {
  const resp = await fetch(`${API_BASE}/scope/preview`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  await assertOk(resp);
  const data = (await resp.json()) as { agents: ScopeAgentBill[] };
  return data.agents;
}
