import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ModelContentDelta,
  ModelReasoningDelta,
  ServerEventUnion,
  ToolCallEndEvent,
  ToolCallStartEvent,
  TurnBlock,
  UIMessage,
} from "../types/events";
import { eventsToMessages } from "../types/events";
import { WebSocketClient, buildWsUrl } from "../lib/ws-client";
import { fetchAllMessages } from "../lib/api";

let _nextId = 0;
function nextId(): string {
  _nextId += 1;
  return `msg_${_nextId}`;
}

export interface UseWebUIStreamResult {
  messages: UIMessage[];
  isStreaming: boolean;
  connect: () => void;
  disconnect: () => void;
  send: (content: string) => void;
}

export function useWebUIStream(
  conversationId: string | null,
): UseWebUIStreamResult {
  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [isStreaming, setIsStreaming] = useState(false);
  const clientRef = useRef<WebSocketClient | null>(null);
  // Track optimistic message content so we can deduplicate the server echo.
  const optimisticContentRef = useRef<string | null>(null);

  const handleEvent = useCallback((event: ServerEventUnion): void => {
    switch (event.event) {
      case "user_message": {
        if (event.content === optimisticContentRef.current) {
          optimisticContentRef.current = null;
          break;
        }
        setMessages((prev) => [
          ...prev,
          {
            id: nextId(),
            role: "user" as const,
            agent_name: event.agent_name,
            blocks: [{ kind: "text" as const, text: event.content }],
            isStreaming: false,
          },
        ]);
        break;
      }

      case "model_content_delta": {
        const delta = event as ModelContentDelta;
        setMessages((prev) => {
          // Find the last streaming message for THIS agent (not just any agent).
          const lastIdx = prev.findLastIndex(
            (m) =>
              m.role === "assistant" &&
              m.agent_name === delta.agent_name &&
              m.isStreaming,
          );
          if (lastIdx >= 0) {
            const last = prev[lastIdx];
            const blocks = [...last.blocks];
            const lastBlock = blocks[blocks.length - 1];
            if (lastBlock && lastBlock.kind === "text") {
              blocks[blocks.length - 1] = {
                ...lastBlock,
                text: lastBlock.text + delta.text,
              };
            } else {
              blocks.push({ kind: "text", text: delta.text });
            }
            return [...prev.slice(0, lastIdx), { ...last, blocks }];
          }
          return [
            ...prev,
            {
              id: nextId(),
              role: "assistant" as const,
              agent_name: delta.agent_name,
              blocks: [{ kind: "text" as const, text: delta.text }],
              isStreaming: true,
            },
          ];
        });
        setIsStreaming(true);
        break;
      }

      case "model_reasoning_delta": {
        const delta = event as ModelReasoningDelta;
        setMessages((prev) => {
          const lastIdx = prev.findLastIndex(
            (m) =>
              m.role === "assistant" &&
              m.agent_name === delta.agent_name &&
              m.isStreaming,
          );
          if (lastIdx >= 0) {
            const last = prev[lastIdx];
            const blocks = [...last.blocks];
            const lastBlock = blocks[blocks.length - 1];
            if (lastBlock && lastBlock.kind === "reasoning") {
              blocks[blocks.length - 1] = {
                ...lastBlock,
                text: lastBlock.text + delta.text,
              };
            } else {
              blocks.push({ kind: "reasoning", text: delta.text });
            }
            return [...prev.slice(0, lastIdx), { ...last, blocks }];
          }
          return [
            ...prev,
            {
              id: nextId(),
              role: "assistant" as const,
              agent_name: delta.agent_name,
              blocks: [{ kind: "reasoning" as const, text: delta.text }],
              isStreaming: true,
            },
          ];
        });
        setIsStreaming(true);
        break;
      }

      case "tool_call_start": {
        const start = event as ToolCallStartEvent;
        setMessages((prev) => {
          const toolBlock: TurnBlock = {
            kind: "tool",
            tool: { tool: start.tool, args: start.args },
          };
          const lastIdx = prev.findLastIndex(
            (m) =>
              m.role === "assistant" &&
              m.agent_name === start.agent_name &&
              m.isStreaming,
          );
          if (lastIdx >= 0) {
            const last = prev[lastIdx];
            return [
              ...prev.slice(0, lastIdx),
              { ...last, blocks: [...last.blocks, toolBlock] },
            ];
          }
          return [
            ...prev,
            {
              id: nextId(),
              role: "assistant" as const,
              agent_name: start.agent_name,
              blocks: [toolBlock],
              isStreaming: true,
            },
          ];
        });
        break;
      }

      case "tool_call_end": {
        const end = event as ToolCallEndEvent;
        setMessages((prev) => {
          const lastIdx = prev.findLastIndex(
            (m) => m.role === "assistant" && m.agent_name === end.agent_name,
          );
          if (lastIdx < 0) return prev;
          const last = prev[lastIdx];
          const blocks = last.blocks.map((b) => {
            if (
              b.kind === "tool" &&
              b.tool.tool === end.tool &&
              b.tool.result === undefined
            ) {
              return {
                ...b,
                tool: { ...b.tool, result: end.result_summary },
              };
            }
            return b;
          });
          return [...prev.slice(0, lastIdx), { ...last, blocks }];
        });
        break;
      }

      case "turn_end": {
        setMessages((prev) => {
          const lastIdx = prev.findLastIndex(
            (m) => m.role === "assistant" && m.isStreaming,
          );
          if (lastIdx >= 0) {
            const last = prev[lastIdx];
            return [
              ...prev.slice(0, lastIdx),
              { ...last, isStreaming: false },
            ];
          }
          return prev;
        });
        setIsStreaming(false);
        break;
      }

      default:
        break;
    }
  }, []);

  const connect = useCallback((): void => {
    if (clientRef.current) {
      clientRef.current.disconnect();
    }
    const client = new WebSocketClient(buildWsUrl(), handleEvent);
    clientRef.current = client;
    client.connect();
  }, [handleEvent]);

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

  // Attach + load history when conversationId changes
  useEffect(() => {
    if (!conversationId) {
      return;
    }
    // Load historical transcript from REST API (all agents merged).
    let cancelled = false;
    fetchAllMessages(conversationId)
      .then((events) => {
        if (cancelled) return;
        const history = eventsToMessages(events);
        setMessages(history);
        setIsStreaming(false);
      })
      .catch(() => {
        // API may not be available — start fresh.
      });

    if (clientRef.current?.connected) {
      clientRef.current.attach(conversationId);
    }

    return (): void => {
      cancelled = true;
    };
  }, [conversationId]);

  const send = useCallback(
    (content: string): void => {
      if (!conversationId) {
        console.warn("Cannot send message: no conversation selected");
        return;
      }
      optimisticContentRef.current = content;
      setMessages((prev) => [
        ...prev,
        {
          id: nextId(),
          role: "user" as const,
          agent_name: "main",
          blocks: [{ kind: "text" as const, text: content }],
          isStreaming: false,
        },
      ]);

      const client = clientRef.current;
      if (!client || !client.connected) {
        console.warn("WebSocket: not connected");
        return;
      }
      client.send("send_message", {
        conversation_id: conversationId,
        content,
      });
    },
    [conversationId],
  );

  return {
    messages,
    isStreaming,
    connect,
    disconnect,
    send,
  };
}
