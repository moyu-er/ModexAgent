// Scope declaration REST client (ticket 16). Mirrors the backend routes in
// `bot/webui/routes/scope_routes.py`: /api/scope/declaration (GET/PUT),
// /api/scope/topology, /api/scope/bill. The bill recomputes from the
// on-disk YAML per request (no cache, SPEC §3.4).
//
// Workspace targeting (PA-10): every endpoint accepts an optional `ws`
// (empty/undefined = home, matching the backend `?ws=` convention). The
// setting is threaded through the existing `appendWsParam` helper so REST
// reads/writes/preview/save all resolve the SAME workspace — a draft
// previewed against one workspace is saved there too. Callers that omit
// `ws` keep the previous home-workspace behavior byte-for-byte.

import { assertOk, API_BASE } from "./api";
import { appendWsParam } from "./url";

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
  targets: string[];
}

export interface ScopeHookBill {
  hook: string;
  /** Origin: position_default | capability_derived | local_hooks. */
  origin: string;
  /** Set when capability_derived — the carrying capability's name. */
  capability: string | null;
}

export interface ScopeToolGroupVariant {
  name: string;
  tools: string[];
}

/** Candidate runtime variants; compilation does not select one. */
export interface ScopeToolGroupManifest {
  anchor: string;
  origin: string;
  capability: string | null;
  variants: ScopeToolGroupVariant[];
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

export interface ScopeMemoryEffective {
  /** Position-derived memory family: "archive_core" (root) | "session_only". */
  memory_preset: string;
  archive_enabled: boolean;
  core_enabled: boolean;
}

export interface ScopeApprovalEffective {
  enabled: boolean;
  /** Root-only eligibility (V9): non-roots cannot enable approval. */
  eligible: boolean;
}

export interface ScopeAgentBill {
  pool: string;
  agent: string;
  root: boolean;
  /** External (Pi/OpenCode) agent — structurally excluded from native
   *  memory/approval/capability assembly; the friendly form hides those. */
  external: boolean;
  fields: ScopeFieldBill[];
  tools: ScopeToolBill[];
  tool_groups: ScopeToolGroupManifest[];
  hooks: ScopeHookBill[];
  capabilities: ScopeCapabilityBill[];
  /** Effective memory toggles from the compiled position defaults. */
  memory: ScopeMemoryEffective;
  /** Effective approval state (declaration + position eligibility). */
  approval: ScopeApprovalEffective;
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

export type ScopeConfigValueType =
  | "string"
  | "boolean"
  | "integer"
  | "number"
  | "array"
  | "object";

export interface ScopeCapabilityConfigField {
  value_type: ScopeConfigValueType;
  default: unknown;
  choices: unknown[];
}

/** What one capability carries — the bundle unit (ADR-0047). */
export interface ScopeCapabilityBundle {
  /** Fixed tools only; group anchors are represented by tool_groups. */
  tools: string[];
  tool_groups: ScopeToolGroupManifest[];
  hooks: string[];
  config_fields: Record<string, ScopeCapabilityConfigField>;
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
//
// Every function takes an optional trailing `ws` ("" / undefined = home).
// The param is appended via `appendWsParam` — the shared `?ws=` convention
// the sessions/attachment APIs already use, so scope reads, previews, and
// saves can never mix workspaces within one editing session.

export async function getScopeDeclaration(ws?: string): Promise<string> {
  const resp = await fetch(appendWsParam(`${API_BASE}/scope/declaration`, ws));
  await assertOk(resp);
  const data = (await resp.json()) as { yaml: string };
  return data.yaml;
}

export async function saveScopeDeclaration(
  yaml: string,
  ws?: string,
): Promise<ScopeDeclarationSave> {
  const resp = await fetch(appendWsParam(`${API_BASE}/scope/declaration`, ws), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ yaml }),
  });
  await assertOk(resp);
  return resp.json() as Promise<ScopeDeclarationSave>;
}

export async function getScopeTopology(ws?: string): Promise<ScopeTopology> {
  const resp = await fetch(appendWsParam(`${API_BASE}/scope/topology`, ws));
  await assertOk(resp);
  return resp.json() as Promise<ScopeTopology>;
}

export async function getScopeBill(ws?: string): Promise<ScopeAgentBill[]> {
  const resp = await fetch(appendWsParam(`${API_BASE}/scope/bill`, ws));
  await assertOk(resp);
  const data = (await resp.json()) as { agents: ScopeAgentBill[] };
  return data.agents;
}

export async function getScopeModel(ws?: string): Promise<ScopeModelTree> {
  const resp = await fetch(appendWsParam(`${API_BASE}/scope/model`, ws));
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
  ws?: string,
): Promise<ScopeDeclarationSave> {
  const resp = await fetch(appendWsParam(`${API_BASE}/scope/model`, ws), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  await assertOk(resp);
  return resp.json() as Promise<ScopeDeclarationSave>;
}

export async function getScopeOptions(ws?: string): Promise<ScopeOptions> {
  const resp = await fetch(appendWsParam(`${API_BASE}/scope/options`, ws));
  await assertOk(resp);
  return resp.json() as Promise<ScopeOptions>;
}

/**
 * POST the draft tree to /api/scope/preview — the PUT's gate chain (load →
 * validate → compile → validate-effective) WITHOUT the commit. Returns the
 * bill-shaped effective view of the draft; on 400 the ApiError detail is a
 * JSON body with `issues: ScopeModelIssue[]`. Pass the SAME `ws` the draft
 * was loaded under — the compile resolves workspace-anchored resources.
 */
export async function previewScopeModel(
  model: ScopeModelTree,
  ws?: string,
): Promise<ScopeAgentBill[]> {
  const resp = await fetch(appendWsParam(`${API_BASE}/scope/preview`, ws), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model }),
  });
  await assertOk(resp);
  const data = (await resp.json()) as { agents: ScopeAgentBill[] };
  return data.agents;
}
