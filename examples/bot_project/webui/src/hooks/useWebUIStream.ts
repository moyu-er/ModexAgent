import { useCallback, useEffect, useRef, useState } from "react";
import type { ServerEventUnion, TodoItemDTO, UIMessage } from "../types/events";
import { eventsToMessages } from "../types/events";
import { WebSocketClient, buildWsUrl } from "../lib/ws-client";
import { fetchMessages, fetchTodos } from "../lib/api";
import { applyServerEvent, type StreamState } from "./useWebUIStream.reducer";

/** Events that mark the start of an assistant turn (set isStreaming=true). */
const STREAM_START_EVENTS = new Set<string>([
  "model_content_delta",
  "model_reasoning_delta",
  "tool_call_start",
]);

export interface UseWebUIStreamResult {
  messages: UIMessage[];
  isStreaming: boolean;
  isPending: boolean;
  /** Active todos for the currently selected session (pending + in_progress). */
  todos: TodoItemDTO[];
  connect: () => void;
  disconnect: () => void;
  send: (content: string) => void;
  pause: () => void;
}

export function useWebUIStream(
  sessionId: string | null,
  getPoolForUuid?: (uuid: string) => string | undefined,
  onSessionReady?: (uuidPrefix: string, fullSessionId: string) => void,
  onSessionActivity?: (sessionId: string) => void,
  currentWs?: string,
  onSessionCreated?: (sessionId: string, parentSessionId: string | null) => void,
): UseWebUIStreamResult {
  const [state, setState] = useState<StreamState>({
    messages: [],
    isStreaming: false,
    sessionMessages: {},
    sessionStreaming: {},
    todos: {},
  });
  const clientRef = useRef<WebSocketClient | null>(null);
  /** ID of the most recent optimistically-added user message.  The server
   * echoes it back via ``_request_id`` in the envelope metadata so the
   * reducer can deduplicate the echo regardless of content. */
  const pendingRequestRef = useRef<string | null>(null);
  /** Non-selected sessions currently mid-turn. Used to notify the host exactly
   * once per turn (on the false→true streaming transition) so it can reorder
   * the sidebar — including a stale child becoming active again. */
  const streamingSessionsRef = useRef<Set<string>>(new Set());

  const agentName = sessionId ? sessionId.split(".")[1] || "main" : "main";

  // Keep mutable refs to the latest session id, pool resolver, and current
  // workspace so the WebSocket client's onopen callback can attach a pending
  // session even when the socket opens after the selection happened.
  const sessionIdRef = useRef(sessionId);
  sessionIdRef.current = sessionId;
  const getPoolForUuidRef = useRef(getPoolForUuid);
  getPoolForUuidRef.current = getPoolForUuid;
  const currentWsRef = useRef(currentWs);
  currentWsRef.current = currentWs;

  const handleEvent = useCallback(
    (event: ServerEventUnion): void => {
      // A new subagent conversation was spawned — notify the host immediately
      // so the sidebar can render it before any streaming output arrives.
      if (event.event === "conversation_created") {
        onSessionCreated?.(event.session_id, event.parent_session_id ?? null);
        return;
      }
      if (
        event.event === "attached" &&
        sessionId &&
        event.session_id !== sessionId &&
        event.session_id.startsWith(sessionId + ".") &&
        getPoolForUuid?.(sessionId) !== undefined
      ) {
        onSessionReady?.(sessionId, event.session_id);
        return;
      }
      // Notify when a non-selected session starts a turn (first streaming
      // event of that turn) so the sidebar reorders it to the top of its
      // group. This also fires when a long-idle child is reactivated.
      const evSid = event.session_id;
      const isOtherSession = !!evSid && evSid !== sessionId;
      if (
        onSessionActivity &&
        isOtherSession &&
        STREAM_START_EVENTS.has(event.event) &&
        !streamingSessionsRef.current.has(evSid)
      ) {
        streamingSessionsRef.current.add(evSid);
        onSessionActivity(evSid);
      }
      if (isOtherSession && (event.event === "turn_end" || event.event === "error")) {
        streamingSessionsRef.current.delete(evSid);
      }
      setState((prev) =>
        applyServerEvent(prev, event, sessionId, pendingRequestRef),
      );

      // When a todo tool completes, re-fetch the authoritative list from the
      // backend.  result_summary is truncated by the emitter (~200 chars), so
      // the reducer cannot reliably parse a full todo list from it.
      // The fetch endpoint reads directly from the per-session TodoStore.
      if (
        event.event === "tool_call_end" &&
        TODO_TOOL_NAMES_SET.has(event.tool) &&
        event.session_id
      ) {
        fetchTodos(event.session_id, currentWsRef.current).then(
          (items) => {
            setState((prev) => ({
              ...prev,
              todos: { ...prev.todos, [event.session_id]: items },
            }));
          },
        ).catch((err) => {
          console.error("Failed to refresh todos after tool_call_end", err);
        });
      }
    },
    [sessionId, getPoolForUuid, onSessionReady, onSessionActivity, onSessionCreated],
  );

  // Keep a mutable reference to the latest handler so the WebSocket client
  // (created once on mount) always forwards events to the handler for the
  // currently selected session.
  const handleEventRef = useRef(handleEvent);
  handleEventRef.current = handleEvent;

  const wsHandleEvent = useCallback((event: ServerEventUnion): void => {
    handleEventRef.current(event);
  }, []);

  const connect = useCallback((): void => {
    if (clientRef.current) {
      clientRef.current.disconnect();
    }
    const client = new WebSocketClient(
      buildWsUrl(),
      wsHandleEvent,
      () => {
        // Connection lost — the live stream is gone, so clear every streaming
        // flag. Otherwise the UI would be stuck showing the pause/busy state
        // forever (no turn_end will ever arrive over a dead socket).
        streamingSessionsRef.current.clear();
        setState((prev) => ({
          ...prev,
          isStreaming: false,
          sessionStreaming: {},
        }));
      },
      () => {
        // Socket just opened. If the current session is a pending draft,
        // attach it now so the user can send the first message.
        const currentSessionId = sessionIdRef.current;
        const pool = currentSessionId
          ? getPoolForUuidRef.current?.(currentSessionId)
          : undefined;
        if (currentSessionId && pool !== undefined) {
          clientRef.current?.attach(currentSessionId, pool, currentWsRef.current);
        }
      },
    );
    clientRef.current = client;
    client.connect();
  }, [wsHandleEvent]);

  const disconnect = useCallback((): void => {
    if (clientRef.current) {
      clientRef.current.disconnect();
      clientRef.current = null;
    }
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return (): void => {
      disconnect();
    };
  }, [disconnect]);

  // Clear per-session buffers when the workspace changes. Without this,
  // buffered events from the OLD workspace's sessions persist and can leak
  // into the new workspace's chat view when a session with the same id is
  // selected. Also clear the streaming-sessions tracker so stale turn_end
  // events from the old workspace don't confuse the activity notifier.
  useEffect(() => {
    setState((prev) => ({
      messages: prev.messages,
      isStreaming: prev.isStreaming,
      sessionMessages: {},
      sessionStreaming: {},
      todos: {},
    }));
    streamingSessionsRef.current.clear();
  }, [currentWs]);

  // Attach + load history when sessionId changes
  useEffect(() => {
    if (!sessionId) {
      setState((prev) => ({
        messages: [],
        isStreaming: false,
        sessionMessages: prev.sessionMessages,
        sessionStreaming: prev.sessionStreaming,
        todos: prev.todos,
      }));
      return;
    }
    const pool = getPoolForUuid?.(sessionId);
    if (pool !== undefined) {
      // Pending session: attach with uuid_prefix + pool, skip history load
      setState((prev) => ({
        messages: [],
        isStreaming: false,
        sessionMessages: prev.sessionMessages,
        sessionStreaming: prev.sessionStreaming,
        todos: prev.todos,
      }));
      if (clientRef.current?.connected) {
        clientRef.current.attach(sessionId, pool, currentWsRef.current);
      }
      return;
    }
    // Existing session: always load the authoritative backend history, then
    // merge any in-progress (still streaming) turn from the live buffer on
    // top. The backend does NOT persist streaming deltas — only completed
    // turns (user_message + assistant_turn) — so the buffer's only content
    // that the history lacks is the currently-streaming turn. Relying on the
    // buffer alone (as before) showed an incomplete view when the buffer was
    // itself incomplete (mid-stream reconnect / page reload).
    let cancelled = false;
    setState((prev) => ({
      messages: [],
      isStreaming: false,
      sessionMessages: prev.sessionMessages,
      sessionStreaming: prev.sessionStreaming,
      todos: prev.todos,
    }));

    Promise.all([
      fetchMessages(sessionId, currentWs),
      fetchTodos(sessionId, currentWs).catch((err) => {
        console.error("Failed to fetch todos for", sessionId, err);
        return undefined;
      }),
    ])
      .then(([events, fetchedTodos]) => {
        if (cancelled) return;
        const history = eventsToMessages(events);
        // Prefer the dedicated todo endpoint; fall back to scanning history if
        // the endpoint is unavailable or returns nothing.
        const initialTodos: TodoItemDTO[] | undefined =
          fetchedTodos ?? scanHistoryForTodos(history);
        setState((prev) => {
          const buf = prev.sessionMessages[sessionId] || [];
          const streaming = prev.sessionStreaming[sessionId] || false;
          // Append only the unfinished turn from the buffer; completed turns
          // are already covered (authoritatively) by the fetched history.
          const liveTail = streaming ? buf.filter((m) => m.isStreaming) : [];
          return {
            ...prev,
            messages: [...history, ...liveTail],
            isStreaming: streaming,
            todos:
              initialTodos !== undefined
                ? { ...prev.todos, [sessionId]: initialTodos }
                : prev.todos,
          };
        });
      })
      .catch((err) => {
        console.error("Failed to fetch messages for", sessionId, err);
      });

    if (clientRef.current?.connected) {
      clientRef.current.attach(sessionId);
    }

    return (): void => {
      cancelled = true;
    };
  }, [sessionId, getPoolForUuid, currentWs]);

  const isPending = sessionId
    ? getPoolForUuid?.(sessionId) !== undefined
    : false;

  const send = useCallback(
    (content: string): void => {
      if (!sessionId) {
        console.warn("Cannot send message: no session selected");
        return;
      }
      if (getPoolForUuid?.(sessionId) !== undefined) {
        console.warn("Cannot send message: session not yet ready");
        return;
      }
      const requestId = crypto.randomUUID();
      pendingRequestRef.current = requestId;
      setState((prev) => ({
        ...prev,
        messages: [
          ...prev.messages,
          {
            id: requestId,
            role: "user" as const,
            agent_name: agentName,
            blocks: [{ kind: "text" as const, text: content }],
            isStreaming: false,
            timestamp: Date.now(),
          },
        ],
      }));

      const client = clientRef.current;
      if (!client || !client.connected) {
        console.warn("WebSocket: not connected");
        return;
      }
      client.sendMessage(sessionId, content, currentWsRef.current, requestId);
    },
    [sessionId, agentName, getPoolForUuid],
  );

  const pause = useCallback((): void => {
    if (!sessionId) {
      console.warn("Cannot pause: no session selected");
      return;
    }
    if (!state.isStreaming) {
      return;
    }
    const client = clientRef.current;
    if (!client || !client.connected) {
      console.warn("WebSocket: not connected");
      return;
    }
    client.pause(sessionId, currentWsRef.current);
  }, [sessionId, state.isStreaming, currentWsRef.current]);

  return {
    messages: state.messages,
    isStreaming: state.isStreaming,
    isPending,
    todos: sessionId ? state.todos[sessionId] ?? [] : [],
    connect,
    disconnect,
    send,
    pause,
  };
}

const TODO_TOOL_NAMES_SET = new Set(["todo_write", "todo_read"]);

/**
 * Approach (a): scan the loaded assistant history for the most recent tool
 * block whose name is a todo tool AND whose result is present, then parse
 * that result as the active todo list. Returns undefined if no todo history
 * was found.
 *
 * Approach (b) — DEFERRED TODO: if we discover that the persisted history
 * does NOT reliably carry tool results (i.e. ``ToolBlock.tool.result`` is
 * often undefined for older sessions), add a server-side fetch endpoint
 * (e.g. ``GET /sessions/:id/todos``) that reads the TodoStore directly and
 * hydrate from that instead of scanning history. See spec §12.
 */
export function scanHistoryForTodos(history: UIMessage[]): TodoItemDTO[] | undefined {
  for (let i = history.length - 1; i >= 0; i -= 1) {
    const msg = history[i];
    if (!msg || msg.role !== "assistant") continue;
    for (let j = msg.blocks.length - 1; j >= 0; j -= 1) {
      const block = msg.blocks[j];
      if (!block || block.kind !== "tool") continue;
      const t = block.tool;
      if (!TODO_TOOL_NAMES_SET.has(t.tool)) continue;
      if (typeof t.result !== "string" || t.result.length === 0) continue;
      const trimmed = t.result.trim();
      if (!trimmed || trimmed.startsWith("Error:")) continue;
      try {
        const parsed = JSON.parse(trimmed) as unknown;
        if (!Array.isArray(parsed)) continue;
        const items = parsed.filter(
          (x): x is TodoItemDTO =>
            typeof x === "object" &&
            x !== null &&
            typeof (x as { content?: unknown }).content === "string",
        );
        return items;
      } catch {
        continue;
      }
    }
  }
  return undefined;
}
