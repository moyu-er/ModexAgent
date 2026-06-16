import { useCallback, useEffect, useRef, useState } from "react";
import type { ServerEventUnion, UIMessage } from "../types/events";
import { eventsToMessages } from "../types/events";
import { WebSocketClient, buildWsUrl } from "../lib/ws-client";
import { fetchMessages } from "../lib/api";
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
  connect: () => void;
  disconnect: () => void;
  send: (content: string) => void;
}

export function useWebUIStream(
  sessionId: string | null,
  getPoolForUuid?: (uuid: string) => string | undefined,
  onSessionReady?: (uuidPrefix: string, fullSessionId: string) => void,
  onSessionActivity?: (sessionId: string) => void,
): UseWebUIStreamResult {
  const [state, setState] = useState<StreamState>({
    messages: [],
    isStreaming: false,
    sessionMessages: {},
    sessionStreaming: {},
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

  const handleEvent = useCallback(
    (event: ServerEventUnion): void => {
      if (
        event.event === "attached" &&
        sessionId &&
        event.session_id !== sessionId &&
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
    },
    [sessionId, getPoolForUuid, onSessionReady, onSessionActivity],
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
    const client = new WebSocketClient(buildWsUrl(), wsHandleEvent, () => {
      // Connection lost — the live stream is gone, so clear every streaming
      // flag. Otherwise the UI would be stuck showing the pause/busy state
      // forever (no turn_end will ever arrive over a dead socket).
      streamingSessionsRef.current.clear();
      setState((prev) => ({
        ...prev,
        isStreaming: false,
        sessionStreaming: {},
      }));
    });
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

  // Attach + load history when sessionId changes
  useEffect(() => {
    if (!sessionId) {
      setState((prev) => ({
        messages: [],
        isStreaming: false,
        sessionMessages: prev.sessionMessages,
        sessionStreaming: prev.sessionStreaming,
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
      }));
      if (clientRef.current?.connected) {
        clientRef.current.attach(sessionId, pool);
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
    }));

    fetchMessages(sessionId)
      .then((events) => {
        if (cancelled) return;
        const history = eventsToMessages(events);
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
  }, [sessionId, getPoolForUuid]);

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
      client.send("send_message", {
        session_id: sessionId,
        content,
        _request_id: requestId,
      });
    },
    [sessionId, agentName, getPoolForUuid],
  );

  return {
    messages: state.messages,
    isStreaming: state.isStreaming,
    isPending,
    connect,
    disconnect,
    send,
  };
}
