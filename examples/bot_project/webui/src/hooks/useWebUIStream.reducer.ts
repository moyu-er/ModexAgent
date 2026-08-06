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
import { envelopeMetadata, envelopeRequestId } from "../types/events";
import { defaultT, type TFn } from "../i18n";

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
  t: TFn,
): { messages: UIMessage[]; isStreaming: boolean } {
  switch (event.event) {
    case "user_message": {
      // Deduplicate the server echo: match by _request_id carried in the
      // envelope's metadata (set by the frontend on send, echoed by server).
      const echoId = envelopeRequestId(event);
      if (echoId && echoId === pendingRequestRef.current) {
        // Only clear the ref when the echo actually matched the optimistic
        // message in THIS array. In the hero-send race the echo can be
        // routed to the buffer branch (its session_id differs from the
        // still-stale currentSessionId) — the optimistic message lives in
        // state.messages, not in the buffer, so matching against the buffer
        // would silently clear the ref and cause the sessionId-change
        // effect to wipe the optimistic message from state.messages.
        // Keeping the ref here lets that effect preserve the optimistic
        // message until fetchMessages reconciles it with backend history.
        const matched = messages.some((m) => m.id === echoId);
        if (matched) {
          pendingRequestRef.current = null;
        }
        // Carry the echoed attachments (persisted records from the ingest
        // stage) onto the optimistic message so they render after echo.
        const echoAttachments = event.attachments ?? undefined;
        return {
          messages: messages.map((m) =>
            m.id === echoId
              ? {
                  ...m,
                  timestamp: event.timestamp,
                  metadata: envelopeMetadata(event),
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
        { kind: "tool", tool: { tool: start.tool, args: start.args, call_id: start.call_id } },
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
      // Pair by call_id, filling exactly one block: matching by tool name
      // would stamp one result onto every unresolved same-name block (and
      // drop the later results) when a turn runs parallel identical tools.
      // Search ALL of this agent's messages, not just the latest — the
      // block may sit in an earlier message when turns interleave.
      let matched = false;
      const msgs = messages.map((m) => {
        if (matched || m.role !== "assistant" || m.agent_name !== end.agent_name) {
          return m;
        }
        let changed = false;
        const blocks = m.blocks.map((b) => {
          if (
            !matched &&
            b.kind === "tool" &&
            b.tool.call_id === end.call_id &&
            b.tool.result === undefined
          ) {
            matched = true;
            changed = true;
            return { ...b, tool: { ...b.tool, result: end.result_summary } };
          }
          return b;
        });
        return changed ? { ...m, blocks } : m;
      });
      if (matched) {
        // A tool call completing does NOT end the turn — the model may reason
        // about the result and keep emitting. Keep streaming until ``turn_end``.
        return { messages: msgs, isStreaming: true };
      }
      // No matching block: the START never reached this client (e.g. the page
      // was refreshed while the turn was suspended for approval — the call is
      // only persisted at END, and resume re-emits END without START). Append
      // a result-only block so the result renders without a second refresh.
      const appended = _upsertStreamingBlock(messages, end.agent_name, {
        kind: "tool",
        tool: { tool: end.tool, args: {}, result: end.result_summary, call_id: end.call_id },
      });
      return { messages: appended, isStreaming: true };
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
      const text = err.message || t("chat.errorFallback");
      return {
        messages: [...messages, {
          id: nextId(),
          role: "system" as const,
          agent_name: err.agent_name || "",
          blocks: [{ kind: "text" as const, text: `${t("chat.errorPrefix")}${text}` }],
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
  t: TFn = defaultT,
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

  // Some legacy flat events used ``conversation_id`` instead of ``session_id``;
  // every typed ServerEvent has ``session_id``, so the fallback is defensive.
  const sid: string =
    event.session_id ||
    (event as unknown as { conversation_id?: string }).conversation_id ||
    "";

  if (sid && sid !== currentSessionId) {
    // Buffer event for a non-selected session (subagent, etc.)
    const prevMessages = state.sessionMessages[sid] || [];
    const result = _applyEventToMessages(prevMessages, event, pendingRequestRef, t);
    return {
      ...state,
      sessionMessages: { ...state.sessionMessages, [sid]: result.messages },
      sessionStreaming: { ...state.sessionStreaming, [sid]: result.isStreaming },
    };
  }

  // Event for the currently selected session — apply directly.
  // Mirror the streaming flag into the per-session map so the send/pause
  // toggle reflects the true state of whichever session the user switches to.
  const result = _applyEventToMessages(state.messages, event, pendingRequestRef, t);
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
