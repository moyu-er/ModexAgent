import type {
  ApprovalRequestView,
  ConversationInfo,
  CreateConversationResponse,
  ServerEventUnion,
  TodoItemDTO,
} from "../types/events";
import type { MediaConfigResponse, UploadAttachmentResponse } from "../types/attachments";
import type { ConfigPayload } from "../types/config";
import { appendWsParam } from "./url";

export const API_BASE = "/api";

// ── Error handling ──────────────────────────────────────────────────────────

/**
 * Raised when a REST call returns a non-2xx response. Carries the HTTP status
 * plus a best-effort detail string so callers can log (or surface) what failed
 * instead of silently swallowing parse errors or treating an error body as data.
 */
export class ApiError extends Error {
  readonly status: number;
  readonly statusText: string;
  readonly detail: string;

  constructor(status: number, statusText: string, detail: string) {
    super(`API ${status} ${statusText}${detail ? `: ${detail}` : ""}`);
    this.name = "ApiError";
    this.status = status;
    this.statusText = statusText;
    this.detail = detail;
  }
}

/**
 * Throws ApiError if `resp` is not ok. Reads the body only on the error path so
 * the success path can still call `resp.json()` as normal.
 */
export async function assertOk(resp: Response): Promise<void> {
  if (!resp.ok) {
    let detail = "";
    try {
      detail = await resp.text();
    } catch {
      // Body already consumed or unreadable — leave detail empty.
    }
    throw new ApiError(resp.status, resp.statusText, detail);
  }
}

// ── Pool types ──────────────────────────────────────────────────────────────

export interface PoolInfo {
  name: string;
}

export async function fetchPools(): Promise<PoolInfo[]> {
  const resp = await fetch(`${API_BASE}/pools`);
  await assertOk(resp);
  return resp.json() as Promise<PoolInfo[]>;
}

// ── Model choices ───────────────────────────────────────────────────────────

export interface ModelChoice {
  provider_name: string;
  model_name: string;
  default: boolean;
}

export async function fetchModels(): Promise<{ choices: ModelChoice[] }> {
  const resp = await fetch(`${API_BASE}/models`);
  await assertOk(resp);
  return resp.json() as Promise<{ choices: ModelChoice[] }>;
}

// ── Provider model-list fetch ───────────────────────────────────────────────

export interface FetchedModel {
  id: string;
  owned_by?: string | null;
  display_name?: string | null;
}

/**
 * Request body for `POST /api/models/fetch`. Single unified schema: all
 * fields optional. Inline fields take priority; missing fields fall back
 * to the saved provider looked up by `provider_key` in model.yml. After
 * merge, `api_key` and (`base_url` or `models_url`) must be non-empty.
 *
 * The frontend never holds the real saved api_key (it is masked), so when
 * the user has NOT re-typed the key, omit `api_key` and supply
 * `provider_key` — the backend reads the saved key from model.yml.
 */
export interface FetchProviderModelsRequest {
  provider_key?: string;
  base_url?: string;
  api_key?: string;
  interface_format?: string;
  models_url?: string | null;
}

/**
 * Fetch the available model list from a provider's model-list endpoint
 * (e.g. /v1/models). All fields optional; inline fields take priority,
 * missing fields fall back to the saved provider (looked up by
 * `provider_key` in model.yml). See `FetchProviderModelsRequest`.
 */
export async function fetchProviderModels(
  req: FetchProviderModelsRequest,
): Promise<{ models: FetchedModel[] }> {
  const resp = await fetch(`${API_BASE}/models/fetch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
  });
  await assertOk(resp);
  return resp.json() as Promise<{ models: FetchedModel[] }>;
}

// ── Session / conversation ──────────────────────────────────────────────────

export async function fetchSessions(
  ws?: string,
  pool?: string,
): Promise<ConversationInfo[]> {
  const query = new URLSearchParams();
  if (ws) {
    query.set("ws", ws);
  }
  if (pool) {
    query.set("pool", pool);
  }
  const params = query.toString() ? `?${query.toString()}` : "";
  const resp = await fetch(`${API_BASE}/sessions${params}`);
  await assertOk(resp);
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
  await assertOk(resp);
  return resp.json() as Promise<CreateConversationResponse>;
}

export async function deleteConversation(
  sessionId: string,
  ws?: string,
): Promise<{ deleted: string }> {
  // ws (workspace) scopes the delete to the session's workspace, so it removes
  // the transcript + index record from the right workspace, not home.
  const params = ws ? `?ws=${encodeURIComponent(ws)}` : "";
  const resp = await fetch(`${API_BASE}/sessions/${sessionId}${params}`, {
    method: "DELETE",
  });
  await assertOk(resp);
  return resp.json() as Promise<{ deleted: string }>;
}

// ── Messages ────────────────────────────────────────────────────────────────

async function fetchSessionResource<T>(
  sessionId: string,
  ws: string | undefined,
  resource: "messages" | "todos" | "approvals",
): Promise<T> {
  // ws (workspace) scopes the read to the session's workspace — without it the
  // server reads home and a message written under another workspace is lost.
  const params = ws ? `?ws=${encodeURIComponent(ws)}` : "";
  const resp = await fetch(`${API_BASE}/sessions/${sessionId}/${resource}${params}`);
  await assertOk(resp);
  return resp.json() as Promise<T>;
}

export async function fetchMessages(
  sessionId: string,
  ws?: string,
): Promise<ServerEventUnion[]> {
  return fetchSessionResource<ServerEventUnion[]>(sessionId, ws, "messages");
}

export async function fetchTodos(
  sessionId: string,
  ws?: string,
): Promise<TodoItemDTO[]> {
  return fetchSessionResource<TodoItemDTO[]>(sessionId, ws, "todos");
}

export async function fetchApprovals(
  sessionId: string,
  ws?: string,
): Promise<ApprovalRequestView[]> {
  return fetchSessionResource<ApprovalRequestView[]>(sessionId, ws, "approvals");
}

export async function submitApproval(
  sessionId: string,
  toolCallId: string,
  action: "allow" | "deny",
  ws?: string,
): Promise<{ accepted: boolean }> {
  const params = ws ? `?ws=${encodeURIComponent(ws)}` : "";
  const resp = await fetch(`${API_BASE}/sessions/${sessionId}/approvals${params}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tool_call_id: toolCallId, action }),
  });
  await assertOk(resp);
  return resp.json() as Promise<{ accepted: boolean }>;
}

export interface WorkspaceInfo {
  home: string;
  recent: { path: string }[];
  /** Configured timezone (IANA name or fixed offset) for readable-time rendering. */
  timezone?: string;
}

export async function fetchWorkspace(): Promise<WorkspaceInfo> {
  const resp = await fetch(`${API_BASE}/workspace`);
  await assertOk(resp);
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
  await assertOk(resp);
  return resp.json() as Promise<ChangeWorkspaceResult>;
}

export interface PickWorkspaceResult {
  path: string | null;
  success: boolean;
  cwd?: string;
  notice?: string;
}

export async function pickWorkspace(): Promise<PickWorkspaceResult> {
  const resp = await fetch(`${API_BASE}/workspace/pick`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
  await assertOk(resp);
  return resp.json() as Promise<PickWorkspaceResult>;
}

// ── Attachments (ADR-0013) ──────────────────────────────────────────────────

/** Fetch the active MediaConfig limits for composer pre-validation. */
export async function fetchMediaConfig(): Promise<MediaConfigResponse> {
  const resp = await fetch(`${API_BASE}/media/config`);
  await assertOk(resp);
  return resp.json() as Promise<MediaConfigResponse>;
}

/**
 * Upload a file as multipart form-data to the per-session temp-file receiver.
 * Returns a ref ({local_path, filename, size, mime?}) the composer includes in
 * the subsequent WS send_message as an attachment. The perception gate + real
 * persistence run later in the ingest stage.
 */
export async function uploadAttachment(
  sessionId: string,
  file: File,
  ws?: string,
): Promise<UploadAttachmentResponse> {
  const form = new FormData();
  form.append("file", file);
  const params = ws ? `?ws=${encodeURIComponent(ws)}` : "";
  const resp = await fetch(`${API_BASE}/sessions/${sessionId}/attachments${params}`, {
    method: "POST",
    body: form,
  });
  await assertOk(resp);
  return resp.json() as Promise<UploadAttachmentResponse>;
}

/**
 * Build the download URL for an attachment, appending the active workspace.
 * Empty ws means the home workspace (matches the existing ?ws= convention:
 * home requests omit the param so the server reads the canonical home dir).
 */
export function attachmentDownloadUrl(
  sessionId: string,
  attachmentId: string,
  ws?: string,
): string {
  return appendWsParam(
    `${API_BASE}/sessions/${sessionId}/attachments/${attachmentId}`,
    ws,
  );
}

// ── Config domains (ADR: settings/config domain API) ────────────────────────

export async function fetchConfig(domain: string): Promise<ConfigPayload> {
  const resp = await fetch(`${API_BASE}/config/${domain}`);
  await assertOk(resp);
  return resp.json() as Promise<ConfigPayload>;
}

export async function saveConfig(
  domain: string,
  payload: Record<string, unknown>,
): Promise<ConfigPayload> {
  const resp = await fetch(`${API_BASE}/config/${domain}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await assertOk(resp);
  return resp.json() as Promise<ConfigPayload>;
}

export async function restartSystem(): Promise<{ restarting: boolean }> {
  const resp = await fetch(`${API_BASE}/system/restart`, { method: "POST" });
  await assertOk(resp);
  return resp.json() as Promise<{ restarting: boolean }>;
}

