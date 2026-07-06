// Pool tree + prompt REST client. Mirrors the backend routes in
// `bot/webui/server.py`: GET/POST /api/pools, GET/PUT/DELETE/PATCH
// /api/pools/{pool}, GET/PUT /api/pools/{pool}/agents/{agent}/prompt.

import type { PoolSummary, PoolTree, PromptContent } from "../types/pool";
import { ApiError, assertOk, API_BASE } from "./api";

const jsonHeaders = { "Content-Type": "application/json" };

export async function listPools(): Promise<PoolSummary[]> {
  const resp = await fetch(`${API_BASE}/pools`);
  await assertOk(resp);
  return resp.json() as Promise<PoolSummary[]>;
}

export async function getPool(name: string): Promise<PoolTree> {
  const resp = await fetch(`${API_BASE}/pools/${encodeURIComponent(name)}`);
  await assertOk(resp);
  return resp.json() as Promise<PoolTree>;
}

export async function savePool(name: string, tree: PoolTree): Promise<PoolTree> {
  const resp = await fetch(`${API_BASE}/pools/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: jsonHeaders,
    body: JSON.stringify(tree),
  });
  await assertOk(resp);
  return resp.json() as Promise<PoolTree>;
}

export async function createPool(name: string): Promise<PoolTree> {
  const resp = await fetch(`${API_BASE}/pools`, {
    method: "POST",
    headers: jsonHeaders,
    body: JSON.stringify({ name }),
  });
  await assertOk(resp);
  return resp.json() as Promise<PoolTree>;
}

export async function deletePool(name: string): Promise<{ deleted: string }> {
  const resp = await fetch(`${API_BASE}/pools/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  await assertOk(resp);
  return resp.json() as Promise<{ deleted: string }>;
}

export async function renamePool(
  oldName: string,
  newName: string,
): Promise<PoolTree> {
  const resp = await fetch(`${API_BASE}/pools/${encodeURIComponent(oldName)}`, {
    method: "PATCH",
    headers: jsonHeaders,
    body: JSON.stringify({ name: newName }),
  });
  await assertOk(resp);
  return resp.json() as Promise<PoolTree>;
}

export async function getPrompt(
  pool: string,
  agent: string,
): Promise<PromptContent> {
  const resp = await fetch(
    `${API_BASE}/pools/${encodeURIComponent(pool)}/agents/${encodeURIComponent(agent)}/prompt`,
  );
  await assertOk(resp);
  return resp.json() as Promise<PromptContent>;
}

export async function savePrompt(
  pool: string,
  agent: string,
  content: string,
): Promise<PromptContent> {
  const resp = await fetch(
    `${API_BASE}/pools/${encodeURIComponent(pool)}/agents/${encodeURIComponent(agent)}/prompt`,
    {
      method: "PUT",
      headers: jsonHeaders,
      body: JSON.stringify({ content }),
    },
  );
  await assertOk(resp);
  return resp.json() as Promise<PromptContent>;
}

export { ApiError };
