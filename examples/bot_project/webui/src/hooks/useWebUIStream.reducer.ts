import type {
  ApprovalRequestEvent,
  ApprovalRequestView,
  AssistantReasoningEvent,
  AttachmentCardEvent,
  ContentEvent,
  ErrorEvent,
  ModelContentDelta,
  ModelReasoningDelta,
  ServerEventUnion,
  TodoItemDTO,
  ToolCallEndEvent,
  ToolCallStartEvent,
  TurnBlock,
  UIMessage,
} from "../types/events";

let _nextId = 0;

export function nextId(): string {
  _nextId += 1;
  return `msg_${_nextId}`;
}

export interface StreamState {
  messages: UIMessage[];
  isStreaming: boolean;
  /** Per-session message buffers for non-selected sessions (subagents, etc.) */
  sessionMessages: Record<string, UIMessage[]>;
  sessionStreaming: Record<string, boolean>;
  /** Per-session active task list (pending + in_progress), keyed by session_id. */
  todos: Record<string, TodoItemDTO[]>;
  /** Per-session pending approvals, keyed by session_id (push from server, pull from GET). */
  pendingApprovals: Record<string, ApprovalRequestView[]>;
}

interface PendingRequestRef {
  current: string | null;
}

/** Append (or merge) a block onto the last streaming assistant message. */
function _upsertStreamingBlock(
  messages: UIMessage[],
  agentName: string,
  block: TurnBlock,
  mergeWith?: (existing: TurnBlock) => TurnBlock | null,
): UIMessage[] {
  const msgs = [...messages];
  const lastIdx = msgs.findLastIndex(
    (m: UIMessage) => m.role === "assistant" && m.agent_name === agentName && m.isStreaming,
  );
  if (lastIdx >= 0) {
    const last = msgs[lastIdx];
    if (!last) return msgs;
    const blocks = [...last.blocks];
    if (mergeWith) {
      const prev = blocks[blocks.length - 1];
      const merged = prev ? mergeWith(prev) : null;
      if (merged) {
        blocks[blocks.length - 1] = merged;
      } else {
        blocks.push(block);
      }
    } else {
      blocks.push(block);
    }
    msgs[lastIdx] = { ...last, blocks };
  } else {
    msgs.push({
      id: nextId(),
      role: "assistant" as const,
      agent_name: agentName,
      blocks: [block],
      isStreaming: true,
      timestamp: undefined as unknown as number,
    });
  }
  return msgs;
}

function _applyEventToMessages(
  messages: UIMessage[],
  event: ServerEventUnion,
  pendingRequestRef: PendingRequestRef,
): { messages: UIMessage[]; isStreaming: boolean } {
  switch (event.event) {
    case "user_message": {
      // Deduplicate the server echo: match by _request_id carried in the
      // envelope's metadata (set by the frontend on send, echoed by server).
      const raw = event as unknown as Record<string, unknown>;
      const meta = raw["_metadata"] as Record<string, unknown> | undefined;
      const echoId: string | undefined = meta?.["_request_id"] as string | undefined;
      if (echoId && echoId === pendingRequestRef.current) {
        pendingRequestRef.current = null;
        // Carry the echoed attachments (persisted records from the ingest
        // stage) onto the optimistic message so they render after echo.
        const echoAttachments = (raw["attachments"] as UIMessage["attachments"]) ?? undefined;
        return {
          messages: messages.map((m) =>
            m.id === echoId
              ? {
                  ...m,
                  timestamp: event.timestamp,
                  metadata: raw["_metadata"] as Record<string, unknown> | undefined,
                  ...(echoAttachments ? { attachments: echoAttachments } : {}),
                }
              : m,
          ),
          isStreaming: false,
        };
      }
      return {
        messages: [...messages, {
          id: nextId(),
          role: "user" as const,
          agent_name: event.agent_name,
          blocks: [{ kind: "text" as const, text: event.content }],
          isStreaming: false,
          timestamp: event.timestamp,
        }],
        isStreaming: false,
      };
    }
    case "model_content_delta": {
      const delta = event as ModelContentDelta;
      const msgs = _upsertStreamingBlock(messages, delta.agent_name,
        { kind: "text", text: delta.text },
        (prev) => prev.kind === "text" ? { ...prev, text: prev.text + delta.text } : null,
      );
      return { messages: msgs, isStreaming: true };
    }
    case "model_reasoning_delta": {
      const delta = event as ModelReasoningDelta;
      const msgs = _upsertStreamingBlock(messages, delta.agent_name,
        { kind: "reasoning", text: delta.text },
        (prev) => prev.kind === "reasoning" ? { ...prev, text: prev.text + delta.text } : null,
      );
      return { messages: msgs, isStreaming: true };
    }
    case "assistant_reasoning": {
      const delta = event as AssistantReasoningEvent;
      const msgs = _upsertStreamingBlock(messages, delta.agent_name,
        { kind: "reasoning", text: delta.text },
        (prev) => prev.kind === "reasoning" ? { ...prev, text: prev.text + delta.text } : null,
      );
      return { messages: msgs, isStreaming: true };
    }
    case "tool_call_start": {
      const start = event as ToolCallStartEvent;
      const msgs = _upsertStreamingBlock(messages, start.agent_name,
        { kind: "tool", tool: { tool: start.tool, args: start.args } },
      );
      return { messages: msgs, isStreaming: true };
    }
    case "attachment_card": {
      // Outbound attachment_card delta — append as an inline attachment block
      // on the streaming assistant message (same accumulation path as text/
      // tool blocks). The renderer resolves the final download URL (with ws)
      // at render time, so the reducer stays ws-agnostic.
      const card = event as AttachmentCardEvent;
      const msgs = _upsertStreamingBlock(messages, card.agent_name, {
        kind: "attachment",
        card: {
          attachment_id: card.attachment_id,
          kind: card.kind,
          name: card.name,
          size: card.size,
          mime: card.mime,
          download_url: card.download_url,
        },
      });
      return { messages: msgs, isStreaming: true };
    }
    case "tool_call_end": {
      const end = event as ToolCallEndEvent;
      const msgs = [...messages];
      const lastIdx = msgs.findLastIndex(
        (m: UIMessage) => m.role === "assistant" && m.agent_name === end.agent_name,
      );
      if (lastIdx < 0 || !msgs[lastIdx]) return { messages: msgs, isStreaming: false };
      const last = msgs[lastIdx]!;
      msgs[lastIdx] = {
        ...last,
        blocks: last.blocks.map((b) => {
          if (b.kind === "tool" && b.tool.tool === end.tool && b.tool.result === undefined) {
            return { ...b, tool: { ...b.tool, result: end.result_summary } };
          }
          return b;
        }),
      };
      // A tool call completing does NOT end the turn — the model may reason
      // about the result and keep emitting. Keep streaming until ``turn_end``.
      return { messages: msgs, isStreaming: true };
    }
    case "turn_end": {
      const msgs = [...messages];
      const lastIdx = msgs.findLastIndex(
        (m: UIMessage) => m.role === "assistant" && m.isStreaming,
      );
      if (lastIdx >= 0 && msgs[lastIdx]) {
        msgs[lastIdx] = { ...msgs[lastIdx]!, isStreaming: false };
      }
      return { messages: msgs, isStreaming: false };
    }
    case "error": {
      // Surface backend errors (e.g. unsupported command, pool switch denied)
      // as a system notice in the chat — not persisted, visible to the user.
      const err = event as ErrorEvent;
      const text = err.message || "An error occurred";
      return {
        messages: [...messages, {
          id: nextId(),
          role: "system" as const,
          agent_name: err.agent_name || "",
          blocks: [{ kind: "text" as const, text: `⚠ ${text}` }],
          isStreaming: false,
          timestamp: Date.now(),
        }],
        isStreaming: false,
      };
    }
    case "content": {
      // Backend control notices (e.g. "⏹ Agent turn stopped.",
      // "No running agent turn to stop.") arrive as content DeltaEnvelopes
      // (WebSocketOutputAdapter.send wraps OutputMessage). Surface them as a
      // non-streaming system notice so the pause button gives feedback even
      // when there is no active turn to cancel.
      const content = event as ContentEvent;
      const text = content.text ?? "";
      if (!text) return { messages, isStreaming: false };
      return {
        messages: [...messages, {
          id: nextId(),
          role: "system" as const,
          agent_name: content.agent_name ?? "",
          blocks: [{ kind: "text" as const, text }],
          isStreaming: false,
          timestamp: Date.now(),
        }],
        isStreaming: false,
      };
    }
    default:
      return { messages, isStreaming: false };
  }
}

/**
 * Pure reducer: apply a single server event to the current stream state.
 *
 * Events for the currently selected session update ``messages`` directly.
 * Events for OTHER sessions (subagents, sibling agents) are buffered in
 * ``sessionMessages`` so they can be shown when the user switches to that
 * session — no streaming output is lost.
 */
export function applyServerEvent(
  state: StreamState,
  event: ServerEventUnion,
  currentSessionId: string | null,
  pendingRequestRef: PendingRequestRef,
): StreamState {
  if (event.event === "approval_request") {
    const areq = event as ApprovalRequestEvent;
    const sid: string = areq.session_id;
    const view: ApprovalRequestView = {
      tool_call_id: areq.tool_call_id,
      tool_name: areq.tool_name,
      tier: areq.tier,
      arguments: areq.arguments,
      status: areq.status,
    };
    const prev = state.pendingApprovals[sid] ?? [];
    if (prev.some((v) => v.tool_call_id === view.tool_call_id)) {
      return state; // dedupe: same request pushed twice (e.g. on restart)
    }
    return {
      ...state,
      pendingApprovals: { ...state.pendingApprovals, [sid]: [...prev, view] },
    };
  }

  const raw = event as unknown as Record<string, unknown>;
  const sid: string = (raw.session_id as string) || (raw.conversation_id as string) || "";

  if (sid && sid !== currentSessionId) {
    // Buffer event for a non-selected session (subagent, etc.)
    const prevMessages = state.sessionMessages[sid] || [];
    const result = _applyEventToMessages(prevMessages, event, pendingRequestRef);
    return {
      ...state,
      sessionMessages: { ...state.sessionMessages, [sid]: result.messages },
      sessionStreaming: { ...state.sessionStreaming, [sid]: result.isStreaming },
    };
  }

  // Event for the currently selected session — apply directly.
  // Mirror the streaming flag into the per-session map so the send/pause
  // toggle reflects the true state of whichever session the user switches to.
  const result = _applyEventToMessages(state.messages, event, pendingRequestRef);
  return {
    ...state,
    messages: result.messages,
    isStreaming: result.isStreaming,
    sessionStreaming: currentSessionId
      ? { ...state.sessionStreaming, [currentSessionId]: result.isStreaming }
      : state.sessionStreaming,
  };
}

/** Remove a decided approval from the store (called after a successful POST). */
export function clearPendingApproval(
  state: StreamState,
  sessionId: string,
  toolCallId: string,
): StreamState {
  const prev = state.pendingApprovals[sessionId] ?? [];
  return {
    ...state,
    pendingApprovals: {
      ...state.pendingApprovals,
      [sessionId]: prev.filter((v) => v.tool_call_id !== toolCallId),
    },
  };
}
