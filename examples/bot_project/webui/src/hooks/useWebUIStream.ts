import { useCallback, useEffect, useRef, useState } from "react";
import type { ServerEventUnion, UIMessage } from "../types/events";
import { eventsToMessages } from "../types/events";
import { WebSocketClient, buildWsUrl } from "../lib/ws-client";
import { fetchMessages } from "../lib/api";
import { applyServerEvent, nextId, type StreamState } from "./useWebUIStream.reducer";

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
): UseWebUIStreamResult {
  const [state, setState] = useState<StreamState>({
    messages: [],
    isStreaming: false,
    sessionMessages: {},
    sessionStreaming: {},
  });
  const clientRef = useRef<WebSocketClient | null>(null);
  // Track optimistic message content so we can deduplicate the server echo.
  const optimisticContentRef = useRef<string | null>(null);

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
      setState((prev) =>
        applyServerEvent(prev, event, sessionId, optimisticContentRef),
      );
    },
    [sessionId, getPoolForUuid, onSessionReady],
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
    const client = new WebSocketClient(buildWsUrl(), wsHandleEvent);
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
    // Existing session: use buffered messages if available, otherwise fetch
    let cancelled = false;
    let hasBuffer = false;
    setState((prev) => {
      const buf = prev.sessionMessages[sessionId] || [];
      hasBuffer = buf.length > 0;
      if (hasBuffer) {
        return {
          messages: buf,
          isStreaming: prev.sessionStreaming[sessionId] || false,
          sessionMessages: prev.sessionMessages,
          sessionStreaming: prev.sessionStreaming,
        };
      }
      return {
        messages: [],
        isStreaming: false,
        sessionMessages: prev.sessionMessages,
        sessionStreaming: prev.sessionStreaming,
      };
    });

    // Skip API fetch when we already have live buffered messages
    if (!hasBuffer) {
      fetchMessages(sessionId)
        .then((events) => {
          if (cancelled) return;
          const history = eventsToMessages(events);
          setState((prev) => ({
            ...prev,
            messages: prev.sessionMessages[sessionId]?.length
              ? prev.messages  // keep buffered messages if new ones arrived
              : history,
            isStreaming: prev.sessionStreaming[sessionId] || false,
          }));
        })
        .catch((err) => {
          console.error("Failed to fetch messages for", sessionId, err);
        });
    }

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
      optimisticContentRef.current = content;
      setState((prev) => ({
        ...prev,
        messages: [
          ...prev.messages,
          {
            id: nextId(),
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
