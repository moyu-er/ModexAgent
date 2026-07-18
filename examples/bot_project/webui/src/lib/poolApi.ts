// Pool tree REST client. Mirrors the backend routes in
// `bot/webui/server.py`: GET/POST /api/pools, GET/PUT/DELETE
// /api/pools/{pool}, POST/DELETE /api/pools/{pool}/peers[/{peer}].
// Prompt CRUD lives in `promptsApi.ts` against /api/prompts.

import type { PoolSummary, PoolTree } from "../types/pool";
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

export interface PeerPairResult {
  pool_a: PoolTree;
  pool_b: PoolTree;
}

export async function addPeer(
  pool: string,
  peer: string,
): Promise<PeerPairResult> {
  const resp = await fetch(
    `${API_BASE}/pools/${encodeURIComponent(pool)}/peers`,
    {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ peer }),
    },
  );
  await assertOk(resp);
  return resp.json() as Promise<PeerPairResult>;
}

export async function removePeer(
  pool: string,
  peer: string,
): Promise<PeerPairResult> {
  const resp = await fetch(
    `${API_BASE}/pools/${encodeURIComponent(pool)}/peers/${encodeURIComponent(peer)}`,
    {
      method: "DELETE",
    },
  );
  await assertOk(resp);
  return resp.json() as Promise<PeerPairResult>;
}

export { ApiError };
