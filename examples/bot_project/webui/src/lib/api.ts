import type {
  ConversationInfo,
  CreateConversationResponse,
  ServerEventUnion,
} from "../types/events";

const API_BASE = "/api";

// ── Pool types ──────────────────────────────────────────────────────────────

export interface PoolInfo {
  name: string;
}

export async function fetchPools(): Promise<PoolInfo[]> {
  const resp = await fetch(`${API_BASE}/pools`);
  return resp.json() as Promise<PoolInfo[]>;
}

// ── Session / conversation ──────────────────────────────────────────────────

export async function fetchSessions(): Promise<ConversationInfo[]> {
  const resp = await fetch(`${API_BASE}/sessions`);
  return resp.json() as Promise<ConversationInfo[]>;
}

export async function createConversation(
  pool?: string,
): Promise<CreateConversationResponse> {
  const body = pool ? JSON.stringify({ pool }) : undefined;
  const resp = await fetch(`${API_BASE}/sessions`, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body,
  });
  return resp.json() as Promise<CreateConversationResponse>;
}

export async function deleteConversation(
  conversationId: string,
): Promise<{ deleted: string }> {
  const resp = await fetch(`${API_BASE}/sessions/${conversationId}`, {
    method: "DELETE",
  });
  return resp.json() as Promise<{ deleted: string }>;
}

// ── Messages ────────────────────────────────────────────────────────────────

export async function fetchAllMessages(
  conversationId: string,
): Promise<ServerEventUnion[]> {
  const resp = await fetch(
    `${API_BASE}/sessions/${conversationId}/messages?all=true`,
  );
  return resp.json() as Promise<ServerEventUnion[]>;
}

export interface WorkspaceInfo {
  cwd: string;
  home: string;
}

export async function fetchWorkspace(): Promise<WorkspaceInfo> {
  const resp = await fetch(`${API_BASE}/workspace`);
  return resp.json() as Promise<WorkspaceInfo>;
}

export async function fetchMessages(
  conversationId: string,
  agent?: string,
): Promise<ServerEventUnion[]> {
  const params = agent ? `?agent=${encodeURIComponent(agent)}` : "";
  const resp = await fetch(
    `${API_BASE}/sessions/${conversationId}/messages${params}`,
  );
  return resp.json() as Promise<ServerEventUnion[]>;
}
