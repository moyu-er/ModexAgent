/** TypeScript types matching Python WebUIEventType enum values. */

export type WebUIEventType =
  | "user_message"
  | "model_content_delta"
  | "model_reasoning_delta"
  | "tool_call_start"
  | "tool_call_end"
  | "turn_end"
  | "assistant_turn"
  | "conversation_ready"
  | "attached"
  | "conversation_deleted"
  | "error";

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

export interface ToolCallStartEvent extends ServerEvent {
  event: "tool_call_start";
  tool: string;
  args: Record<string, unknown>;
  turn_id: string;
}

export interface ToolCallEndEvent extends ServerEvent {
  event: "tool_call_end";
  tool: string;
  result_summary: string;
  turn_id: string;
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
}

export interface ConversationReadyEvent extends ServerEvent {
  event: "conversation_ready";
  session_id: string;
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

export type ServerEventUnion =
  | UserMessageEvent
  | ModelContentDelta
  | ModelReasoningDelta
  | ToolCallStartEvent
  | ToolCallEndEvent
  | TurnEndEvent
  | AssistantTurnEvent
  | ConversationReadyEvent
  | AttachedEvent
  | ConversationDeletedEvent
  | ErrorEvent;

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

/**
 * Unwrap a structured DeltaEnvelope into a flat ServerEvent so the existing
 * reducer and REST-path code remain unchanged.  Pool, parent_session_id, and
 * metadata are preserved as ``_pool``, ``_parent_session_id``, ``_metadata``
 * for the UIMessage builder to attach.
 */
export function unwrapEnvelope(env: DeltaEnvelope): ServerEventUnion {
  const flat = {
    event: env.event_type,
    session_id: env.session_id,
    agent_name: env.agent_name,
    timestamp: env.timestamp,
    ...env.payload,
    // Tagged fields for UIMessage enrichment (tree, pool display, etc.)
    _pool: env.pool,
    _parent_session_id: env.parent_session_id,
    _metadata: env.metadata,
  } as unknown as ServerEventUnion;
  return flat;
}

// ── REST API types ──────────────────────────────────────────────────────────

export interface ConversationInfo {
  session_id: string;
  agent_name: string;
  pool: string;
  parent_session_id: string | null;
  created_at?: number;
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

export type TurnBlock = TextBlock | ToolBlockData | ReasoningBlockData;

export interface UIMessage {
  id: string;
  role: "user" | "assistant";
  agent_name: string;
  blocks: TurnBlock[];
  isStreaming: boolean;
  timestamp?: number;
  /** Business routing context (attached for tree rendering / pool display). */
  pool?: string;
  parent_session_id?: string | null;
  metadata?: Record<string, unknown>;
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
      });
    } else if (ev.event === "assistant_turn") {
      messages.push({
        id: `hist_${++_histId}`,
        role: "assistant",
        agent_name: ev.agent_name,
        blocks: mergeBlocks((ev.blocks ?? []).map(normalizeBlock)),
        isStreaming: false,
        timestamp: ev.timestamp,
      });
    }
  }
  return messages;
}
