// Global MCP registry REST client. Mirrors GET /api/mcp,
// POST/PUT/DELETE /api/mcp/{server}.
//
// On a 409 "in use" the backend returns `{error: "in use", used_by: [[pool, agent], ...]}`.
// `deleteMcp` surfaces this as a typed `McpInUseError` so callers can render the
// conflict list instead of a generic message.

import type { McpServerEntry } from "../types/pool";
import { ApiError, assertOk, API_BASE } from "./api";

const jsonHeaders = { "Content-Type": "application/json" };

/** Pair of [pool, agent] that still references a server. */
export type McpUsage = [string, string];

/** Raised by deleteMcp when the server is still referenced by an agent. */
export class McpInUseError extends Error {
  readonly status = 409;
  readonly usedBy: Array<[string, string]>;

  constructor(usedBy: Array<[string, string]>) {
    const where = usedBy.map(([p, a]) => `${p}/${a}`).join(", ");
    super(`MCP server in use by: ${where}`);
    this.name = "McpInUseError";
    this.usedBy = usedBy;
  }
}

/**
 * GET /api/mcp. The backend serializes with `by_alias=True`, so the transport
 * discriminator comes over as `type` (the framework model's `transport` field
 * has `alias="type"`). `env` is sent as the field name `env` (the framework
 * model accepts `environment` on input only). `normalizeEntry` accepts both
 * `type`/`transport` and `env`/`environment` so callers always see the
 * canonical `transport`/`env`.
 */
export async function getMcp(): Promise<Record<string, McpServerEntry>> {
  const resp = await fetch(`${API_BASE}/mcp`);
  await assertOk(resp);
  const raw = (await resp.json()) as Record<string, Record<string, unknown>>;
  const out: Record<string, McpServerEntry> = {};
  for (const [name, entry] of Object.entries(raw)) {
    out[name] = normalizeEntry(entry);
  }
  return out;
}

/** Map a raw wire entry onto canonical names (accepts `type`/`transport` and `env`/`environment`). */
function normalizeEntry(entry: Record<string, unknown>): McpServerEntry {
  const out: McpServerEntry = {};
  if (entry.transport !== undefined) {
    out.transport = entry.transport as McpServerEntry["transport"];
  } else if (entry.type !== undefined) {
    out.transport = entry.type as McpServerEntry["transport"];
  }
  if (entry.command !== undefined) out.command = entry.command as string;
  if (entry.args !== undefined) out.args = entry.args as string[];
  if (entry.env !== undefined) {
    out.env = entry.env as Record<string, string>;
  } else if (entry.environment !== undefined) {
    out.env = entry.environment as Record<string, string>;
  }
  if (entry.cwd !== undefined) out.cwd = entry.cwd as string;
  if (entry.url !== undefined) out.url = entry.url as string;
  if (entry.headers !== undefined) {
    out.headers = entry.headers as Record<string, string>;
  }
  if (entry.timeout !== undefined) out.timeout = entry.timeout as number;
  return out;
}

/**
 * Upsert a server entry. We send `type` (the registry.json on-disk key) rather
 * than `transport` so the backend alias maps the field cleanly. PUT is used for
 * both create and update.
 *
 * The backend returns the single persisted entry serialized `by_alias=True`
 * (transport as `type`, env as `env`); `normalizeEntry` maps it onto the
 * canonical `transport`/`env` names.
 */
export async function upsertMcp(
  name: string,
  entry: McpServerEntry,
): Promise<McpServerEntry> {
  const wire: Record<string, unknown> = { ...entry };
  if (entry.transport !== undefined) {
    wire.type = entry.transport;
    delete wire.transport;
  }
  const resp = await fetch(`${API_BASE}/mcp/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify(wire),
  });
  await assertOk(resp);
  const raw = (await resp.json()) as Record<string, unknown>;
  return normalizeEntry(raw);
}

export async function deleteMcp(
  name: string,
): Promise<{ deleted: string }> {
  const resp = await fetch(`${API_BASE}/mcp/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (resp.status === 409) {
    let usedBy: Array<[string, string]> = [];
    try {
      const body = (await resp.json()) as { used_by?: Array<[string, string]> };
      usedBy = body.used_by ?? [];
    } catch {
      // Body unreadable — surface an empty conflict list.
    }
    throw new McpInUseError(usedBy);
  }
  await assertOk(resp);
  return resp.json() as Promise<{ deleted: string }>;
}

export { ApiError };
