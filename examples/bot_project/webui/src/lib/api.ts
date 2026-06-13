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

export async function fetchSessions(
  workspace?: string,
  pool?: string,
): Promise<ConversationInfo[]> {
  const query = new URLSearchParams();
  if (workspace) {
    query.set("workspace", workspace);
  }
  if (pool) {
    query.set("pool", pool);
  }
  const params = query.toString() ? `?${query.toString()}` : "";
  const resp = await fetch(`${API_BASE}/sessions${params}`);
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
  sessionId: string,
): Promise<{ deleted: string }> {
  const resp = await fetch(`${API_BASE}/sessions/${sessionId}`, {
    method: "DELETE",
  });
  return resp.json() as Promise<{ deleted: string }>;
}

// ── Messages ────────────────────────────────────────────────────────────────

export async function fetchMessages(
  sessionId: string,
): Promise<ServerEventUnion[]> {
  const resp = await fetch(`${API_BASE}/sessions/${sessionId}/messages`);
  return resp.json() as Promise<ServerEventUnion[]>;
}

export async function fetchAllMessages(
  sessionId: string,
): Promise<ServerEventUnion[]> {
  return fetchMessages(sessionId);
}

export interface WorkspaceInfo {
  cwd: string;
  home: string;
  is_home: boolean;
}

export async function fetchWorkspace(): Promise<WorkspaceInfo> {
  const resp = await fetch(`${API_BASE}/workspace`);
  return resp.json() as Promise<WorkspaceInfo>;
}

export interface ChangeWorkspaceResult {
  success: boolean;
  cwd: string;
  notice: string;
}

export async function changeWorkspace(
  path: string,
): Promise<ChangeWorkspaceResult> {
  const resp = await fetch(`${API_BASE}/workspace/cd`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return resp.json() as Promise<ChangeWorkspaceResult>;
}

export interface BrowseEntry {
  name: string;
  path: string;
  is_dir: boolean;
}

export interface BrowseResult {
  path: string;
  parent: string;
  entries: BrowseEntry[];
  drives: BrowseEntry[];
}

export async function browseWorkspace(
  path: string,
): Promise<BrowseResult> {
  const params = path ? `?path=${encodeURIComponent(path)}` : "";
  const resp = await fetch(`${API_BASE}/workspace/browse${params}`);
  return resp.json() as Promise<BrowseResult>;
}
