/** TypeScript types matching Python WebUIEventType enum values. */

import type { AttachmentRecord, AttachmentCardPayload } from "./attachments";

export type WebUIEventType =
  | "user_message"
  | "model_content_delta"
  | "model_reasoning_delta"
  | "assistant_reasoning"
  | "tool_call_start"
  | "tool_call_end"
  | "turn_end"
  | "assistant_turn"
  | "conversation_ready"
  | "conversation_created"
  | "attached"
  | "conversation_deleted"
  | "error"
  | "content"
  | "approval_request"
  | "attachment_card";

// ── Server → Client events ──────────────────────────────────────────────────

export interface ServerEvent {
  event: WebUIEventType;
  session_id: string;
  agent_name: string;
  timestamp?: number;
}

export interface UserMessageEvent extends ServerEvent {
  event: "user_message";
  content: string;
  /** Inbound attachment records (serialized Attachment.to_dict()). */
  attachments?: AttachmentRecord[];
}

export interface ModelContentDelta extends ServerEvent {
  event: "model_content_delta";
  text: string;
  turn_id: string;
}

export interface ModelReasoningDelta extends ServerEvent {
  event: "model_reasoning_delta";
  text: string;
  turn_id: string;
}

export interface AssistantReasoningEvent extends ServerEvent {
  event: "assistant_reasoning";
  text: string;
  turn_id: string;
}

export interface ToolCallStartEvent extends ServerEvent {
  event: "tool_call_start";
  tool: string;
  args: Record<string, unknown>;
  turn_id: string;
  /** Pairs this start with exactly one tool_call_end (name alone is
   *  ambiguous when the same tool runs multiple times in one turn). */
  call_id: string;
}

export interface ToolCallEndEvent extends ServerEvent {
  event: "tool_call_end";
  tool: string;
  result_summary: string;
  turn_id: string;
  call_id: string;
  seq?: number;
}

export interface TurnEndEvent extends ServerEvent {
  event: "turn_end";
  turn_id: string;
  latency_ms: number;
}

export interface AssistantTurnEvent extends ServerEvent {
  event: "assistant_turn";
  blocks: TurnBlock[];
  turn_id: string;
  latency_ms: number;
  /** Outbound attachment records the agent produced this turn (SendFileToUserTool). */
  attachments?: AttachmentRecord[];
  /** True when this turn was reconstructed from partial streaming deltas
   * (the turn is still in progress). The frontend renders it as a streaming
   * message and appends live WS deltas on top. */
  is_streaming?: boolean;
}

export interface ConversationReadyEvent extends ServerEvent {
  event: "conversation_ready";
  session_id: string;
}

export interface ConversationCreatedEvent extends ServerEvent {
  event: "conversation_created";
  parent_session_id: string | null;
}

export interface AttachedEvent extends ServerEvent {
  event: "attached";
  session_id: string;
}

export interface ConversationDeletedEvent extends ServerEvent {
  event: "conversation_deleted";
  session_id: string;
}

export interface ErrorEvent extends ServerEvent {
  event: "error";
  message: string;
}

export interface ContentEvent extends ServerEvent {
  event: "content";
  text: string;
}

export interface ApprovalRequestView {
  tool_call_id: string;
  tool_name: string;
  tier: string;
  arguments: Record<string, unknown>;
  status: string;
}

export interface ApprovalRequestEvent extends ServerEvent {
  event: "approval_request";
  tool_call_id: string;
  tool_name: string;
  tier: string;
  arguments: Record<string, unknown>;
  status: string;
}

/**
 * Outbound attachment-card delta. Arrives as a DeltaEnvelope with
 * ``event_type: "attachment_card"``; after unwrapEnvelope the payload fields
 * are spread onto this flat event. The renderer treats it symmetrically with
 * inbound AttachmentRecords (image inline vs file card vs fallback).
 */
export interface AttachmentCardEvent extends ServerEvent {
  event: "attachment_card";
  attachment_id: string;
  kind: "image" | "file";
  name: string;
  size: number;
  mime?: string | null;
  download_url: string;
}

export interface TodoItemDTO {
  content: string;
  status: string;
}

export type ServerEventUnion =
  | UserMessageEvent
  | ModelContentDelta
  | ModelReasoningDelta
  | AssistantReasoningEvent
  | ToolCallStartEvent
  | ToolCallEndEvent
  | TurnEndEvent
  | AssistantTurnEvent
  | ConversationReadyEvent
  | ConversationCreatedEvent
  | AttachedEvent
  | ConversationDeletedEvent
  | ErrorEvent
  | ContentEvent
  | ApprovalRequestEvent
  | AttachmentCardEvent;

// ── Structured transport envelope ─────────────────────────────────────────────

export interface DeltaEnvelope {
  session_id: string;
  agent_name: string;
  event_type: string;
  pool: string;
  parent_session_id: string | null;
  metadata: Record<string, unknown>;
  payload: Record<string, unknown>;
  timestamp?: number;
}

// ── Envelope-injected tagged fields ─────────────────────────────────────────

/**
 * Fields ``unwrapEnvelope`` injects onto a flat event so the reducer / UI can
 * recover routing context that the structured envelope carried but the flat
 * event shape does not. They are optional: legacy flat events (not unwrapped
 * from an envelope) never carry them.
 */
export interface EnvelopeTags {
  /** Pool the event's session belongs to (for tree / pool display). */
  _pool?: string;
  /** Parent session id (for tree rendering). */
  _parent_session_id?: string | null;
  /** Envelope metadata — carries ``_request_id`` for optimistic dedup, etc. */
  _metadata?: Record<string, unknown>;
}

/** A server event that may carry envelope-injected tagged fields. */
export type TaggedServerEvent = ServerEventUnion & EnvelopeTags;

/** Read the ``_request_id`` a sender attached to the envelope metadata, if any. */
export function envelopeRequestId(event: ServerEventUnion | TaggedServerEvent): string | undefined {
  const meta = (event as EnvelopeTags)._metadata;
  const id = meta?._request_id;
  return typeof id === "string" ? id : undefined;
}

/** Read the envelope metadata block, if any. */
export function envelopeMetadata(
  event: ServerEventUnion | TaggedServerEvent,
): Record<string, unknown> | undefined {
  return (event as EnvelopeTags)._metadata;
}

/**
 * Unwrap a structured DeltaEnvelope into a flat ServerEvent so the existing
 * reducer and REST-path code remain unchanged.  Pool, parent_session_id, and
 * metadata are preserved as ``_pool``, ``_parent_session_id``, ``_metadata``
 * for the UIMessage builder to attach.
 */
export function unwrapEnvelope(env: DeltaEnvelope): TaggedServerEvent {
  const flat = {
    event: env.event_type,
    session_id: env.session_id,
    agent_name: env.agent_name,
    timestamp: env.timestamp,
    parent_session_id: env.parent_session_id,
    ...env.payload,
    // Tagged fields for UIMessage enrichment (tree, pool display, etc.)
    _pool: env.pool,
    _parent_session_id: env.parent_session_id,
    _metadata: env.metadata,
  } as unknown as TaggedServerEvent;
  return flat;
}

// ── REST API types ──────────────────────────────────────────────────────────

export interface ConversationInfo {
  session_id: string;
  agent_name: string;
  pool: string;
  parent_session_id: string | null;
  created_at?: number;
  updated_at?: number;
}

export interface CreateConversationResponse {
  session_id: string;
  pool: string;
}

// ── UI model types (built from server events) ───────────────────────────────

export interface ToolTrace {
  tool: string;
  args: Record<string, unknown>;
  result?: string;
  /** Present on streaming blocks (from tool_call_start); history blocks
   *  materialized from the transcript don't carry it. */
  call_id?: string;
}

// ── Ordered content blocks (preserves streaming interleaving) ────────────

export interface TextBlock {
  kind: "text";
  text: string;
}

export interface ToolBlockData {
  kind: "tool";
  tool: ToolTrace;
}

export interface ReasoningBlockData {
  kind: "reasoning";
  text: string;
}

/**
 * An attachment rendered inline in the message stream (outbound attachment_card
 * deltas become one of these, appended to the streaming assistant message).
 * Carries the raw card payload; the renderer resolves the final download URL
 * (appending the active ws) at render time so the reducer stays ws-agnostic.
 */
export interface AttachmentBlockData {
  kind: "attachment";
  card: AttachmentCardPayload;
}

export type TurnBlock = TextBlock | ToolBlockData | ReasoningBlockData | AttachmentBlockData;

export interface UIMessage {
  id: string;
  role: "user" | "assistant" | "system";
  agent_name: string;
  blocks: TurnBlock[];
  isStreaming: boolean;
  timestamp?: number;
  /** Business routing context (attached for tree rendering / pool display). */
  pool?: string;
  parent_session_id?: string | null;
  metadata?: Record<string, unknown>;
  /** Attachment records bound to this message (inbound on user, outbound on assistant). */
  attachments?: AttachmentRecord[];
}

// ── Transcript → UI conversion ────────────────────────────────────────────

let _histId = 0;

/**
 * Normalize a block from the transcript store to the TurnBlock type.
 *
 * Python stores tool blocks in flat format:
 *   {kind: "tool", tool: "read", args: {...}, result: "ok"}
 * TypeScript expects nested ToolTrace:
 *   {kind: "tool", tool: {tool: "read", args: {...}, result: "ok"}}
 */
function normalizeBlock(block: TurnBlock): TurnBlock {
  if (block.kind === "tool") {
    // The JSON from Python may have `tool` as a string (flat format)
    // rather than a nested ToolTrace object.  Bridge both shapes.
    const raw = block as unknown as Record<string, unknown>;
    if (typeof raw["tool"] === "string") {
      return {
        kind: "tool",
        tool: {
          tool: raw["tool"] as string,
          args: (raw["args"] as Record<string, unknown>) ?? {},
          result: raw["result"] as string | undefined,
        },
      };
    }
  }
  return block;
}

/**
 * Merge adjacent same-kind blocks in a TurnBlock array.
 *
 * Old transcript entries may contain many consecutive tiny text/reasoning
 * blocks (one per streaming delta) that were saved before server-side
 * ``_merge_blocks`` was introduced.  Without merging, each tiny block
 * renders as its own narrow div, producing elongated message bubbles.
 */
function mergeBlocks(blocks: TurnBlock[]): TurnBlock[] {
  if (blocks.length < 2) return blocks;
  const first = blocks[0];
  if (!first) return blocks;
  const merged: TurnBlock[] = [first];
  for (let i = 1; i < blocks.length; i++) {
    const block = blocks[i];
    if (!block) continue;
    const prev = merged[merged.length - 1];
    if (!prev) continue;
    if (
      (block.kind === "text" || block.kind === "reasoning") &&
      block.kind === prev.kind
    ) {
      const blockText = (block as TextBlock | ReasoningBlockData).text;
      const prevText = (prev as TextBlock | ReasoningBlockData).text;
      merged[merged.length - 1] = {
        kind: prev.kind,
        text: prevText + blockText,
      } as TurnBlock;
    } else {
      merged.push(block);
    }
  }
  return merged;
}

/** Convert transcript events (from REST API) into UIMessage list. */
export function eventsToMessages(events: ServerEventUnion[]): UIMessage[] {
  const messages: UIMessage[] = [];
  for (const ev of events) {
    if (ev.event === "user_message") {
      messages.push({
        id: `hist_${++_histId}`,
        role: "user",
        agent_name: ev.agent_name,
        blocks: [{ kind: "text", text: ev.content }],
        isStreaming: false,
        timestamp: ev.timestamp,
        attachments: ev.attachments,
      });
    } else if (ev.event === "assistant_reasoning") {
      messages.push({
        id: `hist_${++_histId}`,
        role: "assistant",
        agent_name: ev.agent_name,
        blocks: [{ kind: "reasoning", text: ev.text }],
        isStreaming: false,
        timestamp: ev.timestamp,
      });
    } else if (ev.event === "assistant_turn") {
      messages.push({
        id: `hist_${++_histId}`,
        role: "assistant",
        agent_name: ev.agent_name,
        blocks: mergeBlocks((ev.blocks ?? []).map(normalizeBlock)),
        isStreaming: ev.is_streaming ?? false,
        timestamp: ev.timestamp,
        attachments: ev.attachments,
      });
    }
  }
  return messages;
}
