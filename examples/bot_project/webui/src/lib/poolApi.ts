// Declared pool REST client (ticket 11). The backend route surface is
// read-only (GET /api/pools, backed by the scope declaration); pool trees
// are edited through the scope declaration editor in `scopeApi.ts`
// (PUT /api/scope/declaration — restart-effective, ticket 16).

import type { PoolSummary } from "../types/pool";
import { ApiError, assertOk, API_BASE } from "./api";

export async function listPools(): Promise<PoolSummary[]> {
  const resp = await fetch(`${API_BASE}/pools`);
  await assertOk(resp);
  return resp.json() as Promise<PoolSummary[]>;
}

export { ApiError };
