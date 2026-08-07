import { useCallback, useEffect, useRef, useState } from "react";
import type { ApprovalRequestEvent, ApprovalRequestView, ServerEventUnion, TodoItemDTO, UIMessage } from "../types/events";
import type { OutgoingAttachmentRef } from "../types/attachments";
import { eventsToMessages } from "../types/events";
import { WebSocketClient, buildWsUrl } from "../lib/ws-client";
import { fetchApprovals, fetchMessages, fetchTodos, submitApproval as apiSubmitApproval, uploadAttachment } from "../lib/api";
import { applyServerEvent, clearPendingApproval, type StreamState } from "./useWebUIStream.reducer";
import { useT } from "../i18n";

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
  /** Pending approvals for the currently selected session. */
  pendingApprovals: ApprovalRequestView[];
  /** True while ANY approval decision POST is in flight (derived from the
   *  internal per-tool-call submitting flags). Use to disable every approval
   *  button for the duration of a batch-level decision. */
  isApprovingBatch: boolean;
  /** Live WebSocket connection state — drives the statusline signal dot. */
  isConnected: boolean;
  connect: () => void;
  disconnect: () => void;
  send: (
    content: string,
    attachments?: OutgoingAttachmentRef[],
    providerName?: string,
    modelName?: string,
    /** Hero-mode lazy-upload: files selected before a session exists. Uploaded
     *  in the `attached` handler once the real session id is known, then
     *  transmitted as refs alongside the message. Undefined in normal mode. */
    pendingFiles?: File[],
  ) => void;
  pause: () => void;
  /** POST an allow/deny decision for a pending approval; clears the card on success. */
  submitApproval: (toolCallId: string, action: "allow" | "deny") => void;
}

export function useWebUIStream(
  sessionId: string | null,
  getPoolForUuid?: (uuid: string) => string | undefined,
  onSessionReady?: (uuidPrefix: string, fullSessionId: string) => void,
  onSessionActivity?: (sessionId: string) => void,
  currentWs?: string,
  onSessionCreated?: (sessionId: string, parentSessionId: string | null) => void,
): UseWebUIStreamResult {
  const t = useT();
  const [state, setState] = useState<StreamState>({
    messages: [],
    isStreaming: false,
    sessionMessages: {},
    sessionStreaming: {},
    todos: {},
    pendingApprovals: {},
  });
  /** Per-tool-call submitting flag so the card's buttons disable while a
   *  decision POST is in flight. Keyed by tool_call_id. */
  const [submittingApprovals, setSubmittingApprovals] = useState<Record<string, boolean>>({});
  const [isConnected, setIsConnected] = useState(false);
  const clientRef = useRef<WebSocketClient | null>(null);
  /** ID of the most recent optimistically-added user message.  The server
   * echoes it back via ``_request_id`` in the envelope metadata so the
   * reducer can deduplicate the echo regardless of content. */
  const pendingRequestRef = useRef<string | null>(null);
  /** Stashed ws send for a draft (uuid-prefix) session. When the user sends
   *  from a hero-composer flow, ``send()`` adds the optimistic message
   *  immediately but cannot transmit until the backend assigns the real
   *  session id via the ``attached`` event. This ref holds the payload so
   *  the ``attached`` handler can flush it with the full session id. */
  const pendingWsSendRef = useRef<{
    content: string;
    attachments?: OutgoingAttachmentRef[];
    providerName?: string;
    modelName?: string;
    requestId: string;
    files?: File[];
  } | null>(null);
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
        // Flush a queued ws send from a hero-composer draft send. The
        // backend now has the real session id (event.session_id), so the
        // send_message can finally be transmitted.
        const pending = pendingWsSendRef.current;
        if (pending) {
          pendingWsSendRef.current = null;
          const client = clientRef.current;
          if (client?.connected) {
            // Hero-mode lazy upload: if the queued send carries files
            // selected before the session existed, upload them now (the
            // real session id is available) before transmitting
            // send_message. Upload failure drops the optimistic message
            // and aborts the send — the user can retry.
            const fullSid = event.session_id;
            void (async (): Promise<void> => {
              let refs = pending.attachments;
              if (pending.files && pending.files.length > 0) {
                try {
                  const uploaded = await Promise.all(
                    pending.files.map(
                      (f) => uploadAttachment(fullSid, f, currentWsRef.current),
                    ),
                  );
                  refs = [
                    ...(refs ?? []),
                    ...uploaded.map((r) => ({
                      local_path: r.local_path,
                      filename: r.filename,
                      mime: r.mime ?? undefined,
                    })),
                  ];
                } catch (err) {
                  console.error("Hero attachment upload failed", err);
                  pendingRequestRef.current = null;
                  setState((prev) => ({
                    ...prev,
                    messages: prev.messages.filter((m) => m.id !== pending.requestId),
                  }));
                  return;
                }
              }
              client.sendMessage(
                fullSid,
                pending.content,
                currentWsRef.current,
                pending.requestId,
                refs,
                pending.providerName,
                pending.modelName,
              );
            })();
          }
        }
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
      // Fix 1: guard the reducer's append path against phantom re-adds. If a
      // decision POST for this card is in flight, the card was (or is being)
      // optimistically cleared; a stale approval_request arriving now must NOT
      // re-append it. The in-flight POST plus the next turn_end /
      // approval_request reconcile. This mirrors the fetch-replace guard
      // below and keeps the reducer pure.
      const isApprovalRequestInFlight =
        event.event === "approval_request" &&
        !!submittingApprovals[(event as ApprovalRequestEvent).tool_call_id];
      if (!isApprovalRequestInFlight) {
        setState((prev) =>
          applyServerEvent(prev, event, sessionId, pendingRequestRef, t),
        );
      }

      // When a todo tool completes, re-fetch the authoritative list from the
      // backend.  result_summary is truncated by the emitter (~200 chars), so
      // the reducer cannot reliably parse a full todo list from it.
      // The fetch endpoint reads directly from the per-session TodoStore.
      if (
        event.event === "tool_call_end" &&
        event.session_id &&
        (event.tool === "todo_write" || event.tool === "todo_read")
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

      // Re-fetch the authoritative approval list for a session and replace the
      // cached pending list. Shared by the turn_end and approval_request
      // triggers. When guardInFlight is set, skip the replace if a decision
      // POST is currently in flight for one of the session's pending cards —
      // the optimistic clear in submitApproval already produced the correct
      // view, and a stale fetch (captured earlier) would re-add an
      // already-decided card as a phantom pending; the next event reconciles.
      const refreshApprovals = (sid: string, guardInFlight: boolean): void => {
        fetchApprovals(sid, currentWsRef.current)
          .then((views) => {
            setState((prev) => {
              if (
                guardInFlight &&
                (prev.pendingApprovals[sid] ?? []).some(
                  (v) => submittingApprovals[v.tool_call_id],
                )
              ) {
                return prev;
              }
              return {
                ...prev,
                pendingApprovals: { ...prev.pendingApprovals, [sid]: views },
              };
            });
          })
          .catch((err) => {
            console.error("Failed to refresh approvals", err);
          });
      };

      // turn_end fires when a turn completes — reconcile the approval list.
      // (A suspend never emits turn_end; the approval_request block handles
      // that case.)
      if (event.event === "turn_end" && event.session_id) {
        refreshApprovals(event.session_id, false);
      }

      // The backend emits exactly ONE approval_request on suspend (the first
      // pending request). Repurpose it as the trigger to (a) pull the full
      // authoritative PENDING list — correcting the single-push so every
      // queued request renders — and (b) clear the streaming flag, since a
      // suspend never emits turn_end and the agent is paused, not streaming.
      // The reducer's append still runs (via applyServerEvent above); the
      // fetch-then-replace here wins and produces the authoritative list.
      if (event.event === "approval_request" && event.session_id) {
        const areqSid = event.session_id;
        refreshApprovals(areqSid, true);
        // Clear streaming flags: the agent is paused for approval. Only the
        // top-level flag moves when the suspend is on the selected session;
        // the per-session flag always reflects the suspending session.
        setState((prev) => ({
          ...prev,
          isStreaming: areqSid === sessionId ? false : prev.isStreaming,
          sessionStreaming: { ...prev.sessionStreaming, [areqSid]: false },
        }));
      }
    },
    [sessionId, getPoolForUuid, onSessionReady, onSessionActivity, onSessionCreated, submittingApprovals],
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
        setIsConnected(false);
      },
      () => {
        // Socket just opened. If the current session is a pending draft,
        // attach it now so the user can send the first message.
        setIsConnected(true);
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
      setIsConnected(false);
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
      pendingApprovals: {},
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
        pendingApprovals: prev.pendingApprovals,
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
        pendingApprovals: prev.pendingApprovals,
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
    // Keep an in-flight optimistic user message (hero-send draft just
    // promoted to fullId) across this synchronous clear — otherwise the
    // async fetchMessages below resolves to prev.messages=[] and the
    // optimistic is lost until the backend echo arrives.
    const optimisticId = pendingRequestRef.current;
    setState((prev) => ({
      messages: optimisticId
        ? prev.messages.filter((m) => m.id === optimisticId && m.role === "user")
        : [],
      isStreaming: false,
      sessionMessages: prev.sessionMessages,
      sessionStreaming: prev.sessionStreaming,
      todos: prev.todos,
      pendingApprovals: prev.pendingApprovals,
    }));

    Promise.all([
      fetchMessages(sessionId, currentWs),
      fetchTodos(sessionId, currentWs).catch((err) => {
        console.error("Failed to fetch todos for", sessionId, err);
        return [] as TodoItemDTO[];
      }),
      fetchApprovals(sessionId, currentWs).catch((err) => {
        console.error("Failed to fetch approvals for", sessionId, err);
        return [] as ApprovalRequestView[];
      }),
    ])
      .then(([events, fetchedTodos, fetchedApprovals]) => {
        if (cancelled) return;
        const history = eventsToMessages(events);
        setState((prev) => {
          const buf = prev.sessionMessages[sessionId] || [];
          const streaming = prev.sessionStreaming[sessionId] || false;
          // If history already contains a streaming turn (from the backend's
          // partial delta materialization via load_partial), skip liveTail.
          // The history's streaming message is the authoritative base for
          // the in-progress turn, and new WS deltas will append to it via
          // the reducer's _upsertStreamingBlock. Without this guard, both
          // the history's streaming turn AND the buffered WS messages
          // render, showing the same content twice.
          const historyHasStreaming = history.some((m) => m.isStreaming);
          const liveTail =
            streaming && !historyHasStreaming
              ? buf.filter((m) => m.isStreaming)
              : [];
          // Carry through any user messages from prev.messages that aren't
          // in the fetched history. This covers two races:
          // 1. The optimistic message hasn't been persisted yet (fetchMessages
          //    raced ahead of the WS pipeline) and pendingRequestRef is still
          //    set — the optimisticId path preserves it.
          // 2. The echo arrived and cleared pendingRequestRef, but the backend
          //    still hasn't persisted — without this fallback, the user message
          //    would be dropped when messages is replaced with [...history, ...].
          const optimisticId = pendingRequestRef.current;
          const optimisticMsg = optimisticId
            ? prev.messages.find((m) => m.id === optimisticId && m.role === "user")
            : undefined;
          const optimisticFirst = optimisticMsg?.blocks[0];
          const optimisticText =
            optimisticFirst && optimisticFirst.kind === "text"
              ? optimisticFirst.text
              : undefined;
          const optimisticTail =
            optimisticMsg && optimisticText !== undefined &&
            !history.some(
              (h) =>
                h.role === "user" &&
                h.blocks[0]?.kind === "text" &&
                h.blocks[0].text === optimisticText,
            )
              ? [optimisticMsg]
              : [];
          const historyUserTexts = new Set(
            history
              .filter((h) => h.role === "user" && h.blocks[0]?.kind === "text")
              .map((h) => (h.blocks[0] as { text: string }).text),
          );
          const echoedUserTail = prev.messages.filter(
            (m) =>
              m.role === "user" &&
              !m.isStreaming &&
              m.blocks[0]?.kind === "text" &&
              !historyUserTexts.has((m.blocks[0] as { text: string }).text) &&
              m.id !== optimisticId,
          );
          return {
            ...prev,
            messages: [...history, ...echoedUserTail, ...optimisticTail, ...liveTail],
            isStreaming: streaming,
            todos: { ...prev.todos, [sessionId]: fetchedTodos },
            pendingApprovals: { ...prev.pendingApprovals, [sessionId]: fetchedApprovals },
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
    (
      content: string,
      attachments?: OutgoingAttachmentRef[],
      providerName?: string,
      modelName?: string,
      pendingFiles?: File[],
    ): void => {
      if (!sessionId) {
        console.warn("Cannot send message: no session selected");
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

      // Draft (uuid-prefix) session: the backend cannot accept send_message
      // until it has assigned the real session id. Stash the payload; the
      // `attached` event handler flushes it with the full session id once
      // the backend responds. The optimistic message is already shown above.
      if (getPoolForUuid?.(sessionId) !== undefined) {
        pendingWsSendRef.current = { content, attachments, providerName, modelName, requestId, files: pendingFiles };
        return;
      }

      const client = clientRef.current;
      if (!client || !client.connected) {
        console.warn("WebSocket: not connected");
        return;
      }
      client.sendMessage(
        sessionId,
        content,
        currentWsRef.current,
        requestId,
        attachments,
        providerName,
        modelName,
      );
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

  const submitApproval = useCallback(
    (toolCallId: string, action: "allow" | "deny"): void => {
      if (!sessionId) return;
      setSubmittingApprovals((prev) => ({ ...prev, [toolCallId]: true }));
      apiSubmitApproval(sessionId, toolCallId, action, currentWsRef.current)
        .then(() => {
          setState((prev) => clearPendingApproval(prev, sessionId, toolCallId));
        })
        .catch((err) => {
          console.error("Failed to submit approval", err);
        })
        .finally(() => {
          setSubmittingApprovals((prev) => {
            const next = { ...prev };
            delete next[toolCallId];
            return next;
          });
        });
    },
    [sessionId],
  );

  return {
    messages: state.messages,
    isStreaming: state.isStreaming,
    isPending,
    todos: sessionId ? state.todos[sessionId] ?? [] : [],
    pendingApprovals: sessionId ? state.pendingApprovals[sessionId] ?? [] : [],
    isApprovingBatch: Object.keys(submittingApprovals).length > 0,
    isConnected,
    connect,
    disconnect,
    send,
    pause,
    submitApproval,
  };
}

