/** Attachment-system types for the WebUI (ADR-0013).
 *
 * These mirror the backend contracts:
 *  - GET /api/media/config            -> MediaConfigResponse
 *  - POST /api/sessions/{sid}/attachments -> UploadAttachmentResponse
 *  - inbound: UserMessageEvent.attachments[] / AssistantTurnEvent.attachments[]
 *    (serialized Attachment.to_dict(): {id, kind, name, mime, size, path, locator})
 *  - outbound: attachment_card delta payload (AttachmentCardPayload)
 *
 * The renderer is symmetric (one component, inbound + outbound): an inbound
 * record's download URL is built client-side from its id; an outbound card
 * delta already carries its download_url. Both append the active ``ws``.
 */

// ── REST: GET /api/media/config ────────────────────────────────────────────

export interface MediaConfigResponse {
  max_image_bytes: number;
  max_text_doc_bytes: number;
  session_budget_bytes: number;
  max_outbound_bytes: number;
}

// ── REST: POST /api/sessions/{sid}/attachments ─────────────────────────────

export interface UploadAttachmentResponse {
  local_path: string;
  filename: string;
  size: number;
  mime?: string | null;
}

// ── Inbound attachment record (Attachment.to_dict()) ───────────────────────

/**
 * A persisted attachment record carried in transcript events.
 *
 * ``kind`` is the gate-computed three-way classification
 * (image | extractable_document | other). ``locator`` is an internal
 * read-dispatch switch (media | workspace) and is invisible to rendering —
 * the download endpoint abstracts it.
 */
export interface AttachmentRecord {
  id: string;
  kind: "image" | "extractable_document" | "other";
  name: string;
  mime?: string | null;
  size: number;
  path: string;
  locator: "media" | "workspace";
}

// ── Outbound attachment_card delta payload ─────────────────────────────────

/**
 * Payload of the ``attachment_card`` delta (an outbound agent-produced file).
 *
 * ``kind`` is the renderer's two-way card kind (image | file) — only images
 * render inline. ``download_url`` is the bare path; the renderer appends the
 * active ``ws`` query param.
 */
export interface AttachmentCardPayload {
  attachment_id: string;
  kind: "image" | "file";
  name: string;
  size: number;
  mime?: string | null;
  download_url: string;
}

// ── Composer payload (sent over WS as send_message.attachments) ────────────

/**
 * The attachment ref the composer sends alongside a user message. Matches the
 * shape the backend ``_ws_send_message`` reads to build an AttachmentRef
 * (local_path required; filename / mime optional).
 */
export interface OutgoingAttachmentRef {
  local_path: string;
  filename?: string;
  mime?: string;
}
