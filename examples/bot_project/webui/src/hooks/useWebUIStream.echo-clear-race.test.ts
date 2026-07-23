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

describe("useWebUIStream echo-clears-ref-then-fetch-returns-empty race", () => {
  beforeEach(() => {
    sockets = [];
    vi.mocked(fetchMessages).mockResolvedValue([]);
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

  const findSendMessage = (): Record<string, unknown> | undefined => {
    return getSocket().sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .find((m) => m.action === "send_message");
  };

  /**
   * Reproduces the race where:
   * 1. Hero send adds optimistic message (pendingRequestRef = requestId)
   * 2. `attached` promotes session id → WS send flushed
   * 3. React re-renders with full session id
   * 4. sessionId-change effect fires → keeps optimistic msg, starts fetchMessages
   * 5. Echo arrives (correctly routed to selected branch) → clears ref
   * 6. fetchMessages resolves with [] (backend hasn't persisted yet)
   *    → optimisticTail = [] (ref is null) → messages replaced with []
   *    → USER MESSAGE DISAPPEARS
   *
   * This is a DIFFERENT race than echo-race.test.ts (which has the echo
   * arriving BEFORE the re-render). Here the echo arrives AFTER the re-render
   * but BEFORE fetchMessages resolves.
   */
  it("preserves user message when echo clears ref before fetchMessages resolves empty", async () => {
    const pendingUuid = "race001";
    const fullSessionId = "race001.main";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

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

    // 1. Hero send — optimistic message added
    act(() => {
      result.current.send("my message");
    });

    expect(
      result.current.messages.filter((m) => m.role === "user"),
    ).toHaveLength(1);

    // 2. attached arrives → promotes session, flushes WS send
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

    await waitFor(() => expect(currentSessionId).toBe(fullSessionId));

    // Extract _request_id from the flushed send_message frame
    const sendFrame = findSendMessage();
    expect(sendFrame).toBeDefined();
    const requestId = sendFrame?._request_id as string;

    // 3. React re-renders with full session id → sessionId-change effect fires
    //    The effect keeps the optimistic message and starts fetchMessages.
    //    We control fetchMessages resolution timing via a deferred promise.
    let resolveFetch: (val: unknown[]) => void = () => {};
    const fetchPromise = new Promise<unknown[]>((resolve) => {
      resolveFetch = resolve;
    });
    vi.mocked(fetchMessages).mockReturnValueOnce(fetchPromise as Promise<never>);

    act(() => {
      rerender({ sid: fullSessionId });
    });

    // The effect has fired and fetchMessages is now pending.
    // The optimistic message should still be present.
    await waitFor(() => {
      expect(
        result.current.messages.filter((m) => m.role === "user"),
      ).toHaveLength(1);
    });

    // 4. Echo arrives — correctly routed to the SELECTED branch (session_id
    //    matches currentSessionId). The reducer matches the echo's _request_id
    //    to the optimistic message in state.messages, updates its timestamp,
    //    and CLEARS pendingRequestRef.current.
    act(() => {
      getSocket().receive({
        event_type: "user_message",
        session_id: fullSessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: { _request_id: requestId },
        payload: { content: "my message" },
        timestamp: Date.now(),
      });
    });

    // The echo updated the optimistic message's timestamp — still present.
    expect(
      result.current.messages.filter((m) => m.role === "user"),
    ).toHaveLength(1);

    // 5. fetchMessages resolves with [] — backend hasn't persisted the
    //    user message yet (the HTTP request raced ahead of the WS pipeline).
    //    pendingRequestRef.current is now null (cleared by echo), so
    //    optimisticTail = [] → messages replaced with [...[], ...[], ...[]]
    //    → USER MESSAGE DISAPPEARS (this is the bug).
    await act(async () => {
      resolveFetch([]);
    });
    await Promise.resolve();
    await Promise.resolve();

    // 6. ASSERTION (red on bug, green on fix): the user message MUST survive.
    const userMsgs = result.current.messages.filter((m) => m.role === "user");
    expect(userMsgs.length).toBeGreaterThanOrEqual(1);
    expect(userMsgs[0]?.blocks[0]).toMatchObject({
      kind: "text",
      text: "my message",
    });
  });
});
