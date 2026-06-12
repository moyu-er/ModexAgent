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
  conversation_id: string;
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
  conversation_id: string;
}

export interface AttachedEvent extends ServerEvent {
  event: "attached";
  conversation_id: string;
}

export interface ConversationDeletedEvent extends ServerEvent {
  event: "conversation_deleted";
  conversation_id: string;
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

// ── REST API types ──────────────────────────────────────────────────────────

export interface ConversationInfo {
  conversation_id: string;
  agents: string[];
  pool: string;
}

export interface CreateConversationResponse {
  conversation_id: string;
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
function normalizeBlock(block: Record<string, unknown>): TurnBlock {
  if (block["kind"] === "tool" && typeof block["tool"] === "string") {
    return {
      kind: "tool",
      tool: {
        tool: block["tool"] as string,
        args: (block["args"] as Record<string, unknown>) ?? {},
        result: block["result"] as string | undefined,
      },
    };
  }
  return block as unknown as TurnBlock;
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
      });
    } else if (ev.event === "assistant_turn") {
      messages.push({
        id: `hist_${++_histId}`,
        role: "assistant",
        agent_name: ev.agent_name,
        blocks: (ev.blocks ?? []).map(normalizeBlock),
        isStreaming: false,
      });
    }
  }
  return messages;
}
