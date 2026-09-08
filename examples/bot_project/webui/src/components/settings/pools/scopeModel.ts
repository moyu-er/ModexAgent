// scopeModel.ts — typed accessors + tree derivation over the declaration's
// open JSON mapping (GET /api/scope/model returns the verbatim yaml.safe_load
// tree; the backend loader/validator chain owns the shape). All edits flow
// through PoolsConfigView's clone-then-mutate `update()` so React state stays
// immutable at the top level while mutation code here stays readable.

import type { ScopeModelTree } from "../../../lib/scopeApi";

export type AgentBody = Record<string, unknown>;
export type PoolBody = Record<string, unknown>;
export type WorkspaceBody = Record<string, unknown>;

export interface AgentTreeNode {
  name: string;
  /** Agent names from the pool root down to this node (path[0] = root). */
  path: string[];
  body: AgentBody;
  children: AgentTreeNode[];
}

export interface PoolEntry {
  name: string;
  body: PoolBody;
  /** Top-level entries of the pool's agents map; the first is the root. */
  agents: AgentTreeNode[];
}

export interface ModelView {
  /** True for a pool-as-root declaration (`{"pool": {...}}`). */
  poolAsRoot: boolean;
  workspaceName: string | null;
  workspaceBody: WorkspaceBody | null;
  pools: PoolEntry[];
}

// ── Node ids (selection + issue mapping) ────────────────────────────────────

export const WORKSPACE_NODE_ID = "workspace";

export function poolNodeId(pool: string): string {
  return `pool/${pool}`;
}

export function agentNodeId(pool: string, path: string[]): string {
  return `agent/${pool}/${path.join("/")}`;
}

// ── Reading ──────────────────────────────────────────────────────────────────

function asMap(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

export function asNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string")
    : [];
}

function buildAgentNode(name: string, body: AgentBody, parentPath: string[]): AgentTreeNode {
  const path = [...parentPath, name];
  const childrenMap = asMap(body.agents) ?? {};
  return {
    name,
    path,
    body,
    children: Object.entries(childrenMap).map(([childName, childBody]) =>
      buildAgentNode(childName, asMap(childBody) ?? {}, path),
    ),
  };
}

function poolEntry(name: string, body: PoolBody): PoolEntry {
  const agentsMap = asMap(body.agents) ?? {};
  return {
    name,
    body,
    agents: Object.entries(agentsMap).map(([agentName, agentBody]) =>
      buildAgentNode(agentName, asMap(agentBody) ?? {}, []),
    ),
  };
}

export function viewModel(model: ScopeModelTree): ModelView {
  const workspace = asMap(model.workspace);
  if (workspace) {
    const poolsMap = asMap(workspace.pools) ?? {};
    return {
      poolAsRoot: false,
      workspaceName: asString(workspace.name) || null,
      workspaceBody: workspace,
      pools: Object.entries(poolsMap).map(([name, body]) =>
        poolEntry(name, asMap(body) ?? {}),
      ),
    };
  }
  const pool = asMap(model.pool);
  if (pool) {
    const name = asString(pool.name) || "pool";
    return {
      poolAsRoot: true,
      workspaceName: null,
      workspaceBody: null,
      pools: [poolEntry(name, pool)],
    };
  }
  return { poolAsRoot: false, workspaceName: null, workspaceBody: null, pools: [] };
}

/** Find one agent node by pool + path within a viewed model. */
export function findAgent(
  view: ModelView,
  pool: string,
  path: string[],
): AgentTreeNode | null {
  const poolEntry_ = view.pools.find((p) => p.name === pool);
  if (!poolEntry_) return null;
  let nodes = poolEntry_.agents;
  let found: AgentTreeNode | null = null;
  for (const name of path) {
    found = nodes.find((n) => n.name === name) ?? null;
    if (!found) return null;
    nodes = found.children;
  }
  return found;
}

/** All node ids whose display name matches (issue.node is a bare name). */
export function nodeIdsByName(view: ModelView, name: string): string[] {
  const ids: string[] = [];
  if (view.workspaceName === name) ids.push(WORKSPACE_NODE_ID);
  for (const pool of view.pools) {
    if (pool.name === name) ids.push(poolNodeId(pool.name));
    const walk = (nodes: AgentTreeNode[]): void => {
      for (const node of nodes) {
        if (node.name === name) ids.push(agentNodeId(pool.name, node.path));
        walk(node.children);
      }
    };
    walk(pool.agents);
  }
  return ids;
}

// ── Mutating lookups (operate on a cloned draft) ────────────────────────────

/** The mutable pools map inside a draft (workspace form only). */
export function poolsMapOf(draft: ScopeModelTree): Record<string, PoolBody> | null {
  const workspace = asMap(draft.workspace);
  if (!workspace) return null;
  let pools = asMap(workspace.pools) as Record<string, PoolBody> | null;
  if (!pools) {
    pools = {};
    workspace.pools = pools;
  }
  return pools;
}

/** The mutable body of one agent inside a draft. */
export function agentBodyOf(
  draft: ScopeModelTree,
  pool: string,
  path: string[],
): AgentBody | null {
  const view = viewModel(draft);
  return findAgent(view, pool, path)?.body ?? null;
}

export function addPool(draft: ScopeModelTree, name: string): void {
  const pools = poolsMapOf(draft);
  if (!pools) return;
  pools[name] = {
    agents: {
      // Every pool needs exactly one root (V3); create it named after the
      // pool, terminal flags explicit per the declaration convention.
      [name]: { description: "", use_terminal: false, terminal_visibility: false },
    },
  };
}

export function addSubagent(
  draft: ScopeModelTree,
  pool: string,
  parentPath: string[],
  name: string,
): void {
  const parent = agentBodyOf(draft, pool, parentPath);
  if (!parent) return;
  const children = asMap(parent.agents) ?? {};
  children[name] = { description: "" };
  parent.agents = children;
}

export function deletePool(draft: ScopeModelTree, name: string): void {
  const pools = poolsMapOf(draft);
  if (!pools) return;
  delete pools[name];
  // Keep the bidirectional peer invariant: drop every back-reference.
  for (const body of Object.values(pools)) {
    const peers = asStringList(body.peers).filter((p) => p !== name);
    if (peers.length > 0) body.peers = peers;
    else delete body.peers;
  }
}

/** Delete an agent subtree; deleting a pool root deletes the whole pool. */
export function deleteAgent(draft: ScopeModelTree, pool: string, path: string[]): void {
  if (path.length <= 1) {
    deletePool(draft, pool);
    return;
  }
  const parent = agentBodyOf(draft, pool, path.slice(0, -1));
  if (!parent) return;
  const children = asMap(parent.agents);
  if (!children) return;
  delete children[path[path.length - 1]!];
}

/** Write both sides of a bidirectional peer edge (ADR-0019 V5 by construction). */
export function setPeer(draft: ScopeModelTree, a: string, b: string, on: boolean): void {
  const pools = poolsMapOf(draft);
  if (!pools) return;
  for (const [from, to] of [
    [a, b],
    [b, a],
  ] as const) {
    const body = pools[from];
    if (!body) continue;
    const peers = new Set(asStringList(body.peers));
    if (on) peers.add(to);
    else peers.delete(to);
    if (peers.size > 0) body.peers = [...peers].sort();
    else delete body.peers;
  }
}

/** Copy the permissions block onto every other NATIVE pool root agent. */
export function applyPermissionsToOtherPools(
  draft: ScopeModelTree,
  sourcePool: string,
  sourcePath: string[],
): void {
  const source = agentBodyOf(draft, sourcePool, sourcePath);
  if (!source) return;
  const view = viewModel(draft);
  for (const pool of view.pools) {
    for (const top of pool.agents) {
      if (pool.name === sourcePool && top.path.join("/") === sourcePath.join("/")) {
        continue;
      }
      // External agents run in the provider CLI — the native permission
      // face (interceptors/approval) is meaningless there.
      if (asString(top.body.execution_strategy) === "external") continue;
      for (const key of ["interceptors", "interceptor_configs", "approval"] as const) {
        if (source[key] === undefined) delete top.body[key];
        else top.body[key] = JSON.parse(JSON.stringify(source[key])) as unknown;
      }
    }
  }
}

// ── Agent field helpers ──────────────────────────────────────────────────────

/** Set a scalar field, omitting the key entirely when `value` is null. */
export function setField(body: AgentBody, key: string, value: unknown): void {
  if (value === null || value === undefined) delete body[key];
  else body[key] = value;
}

// Capabilities: the declaration's override map is tri-state per name —
// absent (follow auto) / {} (force on) / false (force off).
export type CapabilityMode = "auto" | "on" | "off";

export function capabilityMode(body: AgentBody, name: string): CapabilityMode {
  const caps = asMap(body.capabilities);
  if (caps === null || !(name in caps)) return "auto";
  return caps[name] === false ? "off" : "on";
}

export function setCapabilityMode(body: AgentBody, name: string, mode: CapabilityMode): void {
  const caps = { ...(asMap(body.capabilities) ?? {}) };
  if (mode === "auto") delete caps[name];
  else if (mode === "on") caps[name] = {};
  else caps[name] = false;
  setField(body, "capabilities", Object.keys(caps).length > 0 ? caps : null);
}

// Hooks: the declared list carries +/- merge prefixes verbatim. The form
// edits ONLY deviations — a veto ("-name") applies to position defaults and
// capability-bundle hooks alike; an add ("+name") is for free-standing roster
// hooks only (bundle-carried hooks follow their capability and are never
// emitted as "+name" — enforced by the combobox candidate list, C2).

/** Names currently vetoed in the declaration (`-name` entries). */
export function vetoedHooks(body: AgentBody): string[] {
  return asStringList(body.hooks)
    .filter((e) => e.startsWith("-"))
    .map((e) => e.slice(1));
}

/** Names declared as additions (`+name` or plain entries). */
export function declaredHooks(body: AgentBody): string[] {
  return asStringList(body.hooks)
    .filter((e) => !e.startsWith("-"))
    .map((e) => (e.startsWith("+") ? e.slice(1) : e));
}

/** Veto a hook (default or bundle-carried): drops any add entry, writes -name. */
export function vetoHook(body: AgentBody, name: string): void {
  const entries = asStringList(body.hooks).filter(
    (e) => e !== `+${name}` && e !== `-${name}` && e !== name,
  );
  entries.push(`-${name}`);
  setField(body, "hooks", entries);
}

/** Restore a vetoed hook: removes the -name entry. */
export function restoreHook(body: AgentBody, name: string): void {
  const entries = asStringList(body.hooks).filter((e) => e !== `-${name}`);
  setField(body, "hooks", entries.length > 0 ? entries : null);
}

/** Add a free-standing roster hook: drops any veto, writes +name. */
export function addDeclaredHook(body: AgentBody, name: string): void {
  const entries = asStringList(body.hooks).filter(
    (e) => e !== `+${name}` && e !== `-${name}` && e !== name,
  );
  entries.push(`+${name}`);
  setField(body, "hooks", entries);
}

/** Remove a declared add entry (+name or plain). Vetoes are untouched. */
export function removeDeclaredHook(body: AgentBody, name: string): void {
  const entries = asStringList(body.hooks).filter(
    (e) => e !== `+${name}` && e !== name,
  );
  setField(body, "hooks", entries.length > 0 ? entries : null);
}

/**
 * Combobox candidates for adding a hook: the backend roster MINUS every
 * bundle-carried hook (they ride their capability — never declarable) MINUS
 * the already-effective hooks.
 */
export function hookCandidates(
  roster: string[],
  bundleCarried: ReadonlySet<string>,
  effective: ReadonlySet<string>,
): string[] {
  return roster.filter((h) => !bundleCarried.has(h) && !effective.has(h));
}

/** Union of every capability bundle's carried hooks. */
export function bundleCarriedHooks(
  bundles: Record<string, { tools: string[]; hooks: string[] }>,
): Set<string> {
  const out = new Set<string>();
  for (const bundle of Object.values(bundles)) {
    for (const hook of bundle.hooks) out.add(hook);
  }
  return out;
}

// Interceptors: no position defaults and no capability contributions — the
// effective roster IS the declared list ("+name" shipped convention, plain
// accepted). Chips + add-combobox over the registry roster.

/** Declared interceptor names with the merge prefix stripped. */
export function declaredInterceptors(body: AgentBody): string[] {
  return asStringList(body.interceptors).map((e) =>
    e.startsWith("+") ? e.slice(1) : e,
  );
}

export function interceptorOn(body: AgentBody, name: string): boolean {
  return declaredInterceptors(body).includes(name);
}

export function setInterceptor(body: AgentBody, name: string, on: boolean): void {
  const entries = asStringList(body.interceptors).filter(
    (e) => e !== `+${name}` && e !== name,
  );
  if (on) entries.push(`+${name}`);
  setField(body, "interceptors", entries.length > 0 ? entries : null);
  if (!on && name === "sandbox_guard") {
    // Drop the orphaned config block with its interceptor.
    const configs = asMap(body.interceptor_configs);
    if (configs) {
      delete configs[name];
      if (Object.keys(configs).length === 0) delete body.interceptor_configs;
    }
  }
}

export function toggleInListField(body: AgentBody, key: string, name: string, on: boolean): void {
  const list = asStringList(body[key]);
  const next = on ? [...new Set([...list, name])] : list.filter((v) => v !== name);
  setField(body, key, next.length > 0 ? next : null);
}

// ── Nested config accessors (sandbox_guard / approval / memory) ─────────────

export function nestedMap(body: Record<string, unknown>, ...keys: string[]): Record<string, unknown> | null {
  let cur: Record<string, unknown> | null = body;
  for (const key of keys) {
    cur = cur ? asMap(cur[key]) : null;
  }
  return cur;
}

/** Ensure a nested map path exists on a draft body and return it. */
export function ensureNested(body: Record<string, unknown>, ...keys: string[]): Record<string, unknown> {
  let cur = body;
  for (const key of keys) {
    const next = asMap(cur[key]);
    if (next) {
      cur = next;
    } else {
      const created: Record<string, unknown> = {};
      cur[key] = created;
      cur = created;
    }
  }
  return cur;
}
