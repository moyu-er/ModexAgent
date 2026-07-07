// Global + per-agent skills REST client. Mirrors GET/POST/DELETE /api/skills,
// GET /api/pools/{pool}/agents/{agent}/skills, and POST/DELETE
// /api/pools/{pool}/agents/{agent}/skills/{name}.
//
// Upload uses the documented JSON-base64 fallback: POST /api/skills with body
// `{name, files: {relpath: base64}}`. The backend also accepts multipart, but
// the JSON shape is the simpler contract for the webui (no boundary wrangling).

import type { SkillEntry, SkillOrigin, SkillSource } from "../types/pool";
import { ApiError, assertOk, API_BASE } from "./api";

const jsonHeaders = { "Content-Type": "application/json" };

function asString(value: unknown): string {
  if (typeof value !== "string") {
    throw new TypeError(`Expected string, got ${String(value)}`);
  }
  return value;
}

function asSkillSource(value: unknown): SkillSource {
  if (value === "global" || value === "local") return value;
  throw new TypeError(`Invalid skill source: ${String(value)}`);
}

function asSkillOrigin(value: unknown): SkillOrigin | undefined {
  if (value === undefined || value === null || value === "") return undefined;
  if (value === "repo" || value === "user") return value;
  throw new TypeError(`Invalid skill origin: ${String(value)}`);
}

function mapSkillEntry(raw: Record<string, unknown>): SkillEntry {
  let origin: SkillOrigin | undefined;
  try {
    origin = asSkillOrigin(raw.origin);
  } catch (err) {
    // One malformed entry should not break the entire skill list.
    console.warn("Invalid skill origin in API response:", err);
  }
  return {
    name: asString(raw.name),
    source: asSkillSource(raw.source),
    origin,
    description: typeof raw.description === "string" ? raw.description : undefined,
  };
}

export interface SkillFile {
  relpath: string;
  content: string; // base64-encoded bytes
}

export async function listSkills(): Promise<SkillEntry[]> {
  const resp = await fetch(`${API_BASE}/skills`);
  await assertOk(resp);
  const data = (await resp.json()) as Record<string, unknown>[];
  return data.map(mapSkillEntry);
}

export async function uploadSkill(
  name: string,
  files: SkillFile[],
): Promise<SkillEntry> {
  const payload: Record<string, string> = {};
  for (const f of files) {
    payload[f.relpath] = f.content;
  }
  const resp = await fetch(`${API_BASE}/skills`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ name, files: payload }),
  });
  await assertOk(resp);
  const data = (await resp.json()) as Record<string, unknown>;
  return mapSkillEntry(data);
}

export async function deleteSkill(name: string): Promise<{ deleted: string }> {
  const resp = await fetch(`${API_BASE}/skills/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  await assertOk(resp);
  return resp.json() as Promise<{ deleted: string }>;
}

export async function listAgentSkills(
  pool: string,
  agent: string,
): Promise<SkillEntry[]> {
  const resp = await fetch(
    `${API_BASE}/pools/${encodeURIComponent(pool)}/agents/${encodeURIComponent(agent)}/skills`,
  );
  await assertOk(resp);
  const data = (await resp.json()) as Record<string, unknown>[];
  return data.map(mapSkillEntry);
}

export async function assignSkill(
  pool: string,
  agent: string,
  name: string,
): Promise<{ assigned: string }> {
  const resp = await fetch(
    `${API_BASE}/pools/${encodeURIComponent(pool)}/agents/${encodeURIComponent(agent)}/skills/${encodeURIComponent(name)}`,
    { method: "POST" },
  );
  await assertOk(resp);
  return resp.json() as Promise<{ assigned: string }>;
}

export async function unassignSkill(
  pool: string,
  agent: string,
  name: string,
): Promise<{ unassigned: string }> {
  const resp = await fetch(
    `${API_BASE}/pools/${encodeURIComponent(pool)}/agents/${encodeURIComponent(agent)}/skills/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
  await assertOk(resp);
  return resp.json() as Promise<{ unassigned: string }>;
}

export { ApiError };
