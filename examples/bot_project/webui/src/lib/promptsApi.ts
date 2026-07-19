// Global prompts REST client. Mirrors GET /api/prompts, GET /api/prompts/{name},
// PUT /api/prompts/{name} (upsert), POST /api/prompts (create), and
// DELETE /api/prompts/{name} (reference-checked) in `bot/webui/server.py`.

import type { PromptContent, PromptSummary, PromptUsage } from "../types/pool";
import { ApiError, assertOk, API_BASE } from "./api";

export async function listPrompts(): Promise<PromptSummary[]> {
  const resp = await fetch(`${API_BASE}/prompts`);
  await assertOk(resp);
  return resp.json() as Promise<PromptSummary[]>;
}

export async function getPrompt(name: string): Promise<PromptContent> {
  const resp = await fetch(`${API_BASE}/prompts/${encodeURIComponent(name)}`);
  await assertOk(resp);
  return resp.json() as Promise<PromptContent>;
}

export async function savePrompt(
  name: string,
  content: string,
): Promise<PromptContent> {
  const resp = await fetch(`${API_BASE}/prompts/${encodeURIComponent(name)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content }),
  });
  await assertOk(resp);
  return resp.json() as Promise<PromptContent>;
}

export async function createPrompt(
  name: string,
  content?: string,
): Promise<PromptContent> {
  const body = content !== undefined ? { name, content } : { name };
  const resp = await fetch(`${API_BASE}/prompts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await assertOk(resp);
  return resp.json() as Promise<PromptContent>;
}

export class PromptInUseError extends Error {
  readonly usages: PromptUsage[];

  constructor(usages: PromptUsage[]) {
    super("Prompt is in use");
    this.name = "PromptInUseError";
    this.usages = usages;
  }
}

export async function deletePrompt(
  name: string,
): Promise<{ deleted: string }> {
  const resp = await fetch(`${API_BASE}/prompts/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (resp.status === 409) {
    let body: { error?: string; usages?: PromptUsage[] };
    try {
      body = (await resp.json()) as { error?: string; usages?: PromptUsage[] };
    } catch {
      body = {};
    }
    if (body.error === "in_use" && Array.isArray(body.usages)) {
      throw new PromptInUseError(body.usages);
    }
    throw new ApiError(resp.status, resp.statusText, JSON.stringify(body));
  }
  await assertOk(resp);
  return resp.json() as Promise<{ deleted: string }>;
}

export { ApiError };
