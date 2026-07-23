import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWebUIStream } from "./useWebUIStream";
import { fetchMessages } from "../lib/api";

vi.mock("../lib/api", () => ({
  fetchMessages: vi.fn().mockResolvedValue([]),
  fetchTodos: vi.fn().mockResolvedValue([]),
  fetchApprovals: vi.fn().mockResolvedValue([]),
  submitApproval: vi.fn().mockResolvedValue({ accepted: true }),
}));

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  readyState = FakeWebSocket.OPEN;
  onopen: ((this: WebSocket, ev: Event) => void) | null = null;
  onclose: ((this: WebSocket, ev: CloseEvent) => void) | null = null;
  onmessage: ((this: WebSocket, ev: MessageEvent) => void) | null = null;
  sent: string[] = [];
  constructor() {
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.call(this as unknown as WebSocket, new Event("open"));
    });
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
    queueMicrotask(() => {
      this.onclose?.call(this as unknown as WebSocket, new CloseEvent("close"));
    });
  }
  dispatchEvent(): boolean {
    return true;
  }
  receive(data: unknown): void {
    this.onmessage?.call(
      this as unknown as WebSocket,
      new MessageEvent("message", { data: JSON.stringify(data) }),
    );
  }
}

let sockets: FakeWebSocket[] = [];

describe("useWebUIStream hero-send echo race (bug repro)", () => {
  beforeEach(() => {
    sockets = [];
    vi.stubGlobal(
      "WebSocket",
      class extends FakeWebSocket {
        constructor() {
          super();
          sockets.push(this);
        }
      },
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  const getSocket = (): FakeWebSocket => {
    if (sockets.length === 0) {
      throw new Error("No WebSocket created");
    }
    return sockets[sockets.length - 1]!;
  };

  /**
   * Reproduces the non-deterministic bug where, in the hero-send flow, the
   * backend's `user_message` echo (carrying `_metadata._request_id`) arrives
   * in the SAME event-loop tick as the `attached` event — before React has
   * re-rendered with the promoted full session id.
   *
   * Symptom: the optimistic user message the frontend added in `send()` is
   * dropped from `state.messages` after the sessionId-change effect runs,
   * because the echo's dedup path cleared `pendingRequestRef.current` while
   * routing the echo to the wrong (buffer) branch.
   *
   * Expected (post-fix): the user message MUST survive across the promotion,
   * regardless of whether the echo arrives before or after the re-render.
   */
  it("preserves optimistic user message when echo races ahead of fullId re-render", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);

    const pendingUuid = "abc123";
    const fullSessionId = "abc123.main";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    // The hook receives sessionId as a prop driven by the host (useSessions).
    // In the real app, onSessionReady -> setSelectedId(fullSessionId) triggers
    // a re-render that propagates the new id back into the hook. In this test
    // we drive that propagation manually via `rerender` to model the timing.
    let currentSessionId: string | null = pendingUuid;
    const onSessionReady = (_uuidPrefix: string, fullId: string): void => {
      currentSessionId = fullId;
    };

    const { result, rerender } = renderHook(
      ({ sid }: { sid: string | null }) =>
        useWebUIStream(sid, getPoolForUuid, onSessionReady),
      { initialProps: { sid: pendingUuid } },
    );

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    // 1. User submits from the hero composer — send() adds the optimistic
    //    message immediately and queues the ws send for the `attached`
    //    handler to flush with the real session id.
    act(() => {
      result.current.send("hero message");
    });

    const userMsgsBefore = result.current.messages.filter((m) => m.role === "user");
    expect(userMsgsBefore).toHaveLength(1);
    expect(userMsgsBefore[0]?.blocks[0]).toMatchObject({
      kind: "text",
      text: "hero message",
    });

    // 2. Race: `attached` and the `user_message` echo arrive in the SAME
    //    tick (two WS frames dispatched back-to-back before React commits
    //    the state update queued by onSessionReady). The attached handler
    //    flushes the queued send_message synchronously; the echo then hits
    //    the still-old sessionId (pendingUuid) and is mis-routed.
    //
    //    To capture the echo's _request_id we read it off the flushed
    //    send_message frame (the client stamped it there in send()).
    act(() => {
      getSocket().receive({
        event_type: "attached",
        session_id: fullSessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: {},
        payload: {},
      });
    });

    // After `attached`, the queued send_message must have been flushed with
    // the full session id — that frame carries _request_id, which the
    // backend would echo back. Extract it to build a faithful echo frame.
    const sendFrame = getSocket().sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .find((m) => m.action === "send_message");
    expect(sendFrame).toBeDefined();
    expect(sendFrame?.session_id).toBe(fullSessionId);
    const requestId = sendFrame?._request_id;
    expect(typeof requestId).toBe("string");

    // Now the echo arrives BEFORE React re-renders with the promoted id.
    act(() => {
      getSocket().receive({
        event_type: "user_message",
        session_id: fullSessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: { _request_id: requestId },
        payload: { content: "hero message" },
        timestamp: Date.now(),
      });
    });

    // 3. React finally commits the promotion: the host re-renders the hook
    //    with the full session id. This triggers the sessionId-change effect
    //    which (in the buggy version) reads pendingRequestRef.current and,
    //    finding it null (cleared by the mis-routed echo), wipes messages.
    act(() => {
      rerender({ sid: fullSessionId });
    });

    await waitFor(() => {
      expect(currentSessionId).toBe(fullSessionId);
      expect(result.current.isPending).toBe(false);
    });

    // Let any async fetchMessages settle.
    await waitFor(() => {
      expect(vi.mocked(fetchMessages)).toHaveBeenCalled();
    });
    await Promise.resolve();
    await Promise.resolve();

    // 4. ASSERTION (red on bug, green on fix): the user's optimistic message
    //    MUST still be present. On the bug, messages is [] here.
    const userMsgsAfter = result.current.messages.filter((m) => m.role === "user");
    expect(userMsgsAfter.length).toBeGreaterThanOrEqual(1);
    expect(userMsgsAfter[0]?.blocks[0]).toMatchObject({
      kind: "text",
      text: "hero message",
    });
  });
});
