import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWebUIStream } from "./useWebUIStream";
import { fetchMessages } from "../lib/api";

vi.mock("../lib/api", () => ({
  fetchMessages: vi.fn().mockResolvedValue([]),
  fetchTodos: vi.fn().mockResolvedValue([]),
}));

// Fake WebSocket that records every sent frame.
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

describe("useWebUIStream new-conversation attach", () => {
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

  const findAttach = (): Record<string, unknown> | undefined => {
    return getSocket().sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .find((m) => m.action === "attach");
  };

  const findSendMessage = (): Record<string, unknown> | undefined => {
    return getSocket().sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .find((m) => m.action === "send_message");
  };

  it("attaches a pending session when selected", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    const { result } = renderHook(() => useWebUIStream(pendingUuid, getPoolForUuid));

    expect(result.current.isPending).toBe(true);

    act(() => {
      result.current.connect();
    });

    // Wait for the socket to exist.
    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));
    const socket = getSocket();
    expect(socket.readyState).toBe(FakeWebSocket.OPEN);

    // Wait for the hook to attach.
    await waitFor(() => {
      const attach = findAttach();
      expect(attach).toBeDefined();
      expect(attach?.uuid_prefix).toBe(pendingUuid);
      expect(attach?.pool).toBe("main");
    });
  });

  it("sends ws in send_message when currentWs is provided", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    let currentSessionId: string | null = pendingUuid;
    const onSessionReady = (uuidPrefix: string, fullSessionId: string): void => {
      expect(uuidPrefix).toBe(pendingUuid);
      currentSessionId = fullSessionId;
    };

    const { result, rerender } = renderHook(
      ({ sid }: { sid: string | null }) =>
        useWebUIStream(sid, getPoolForUuid, onSessionReady, undefined, "/some/workspace"),
      { initialProps: { sid: pendingUuid } },
    );

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(findAttach()).toBeDefined());

    // Backend promotes the pending uuid to a full session id.
    act(() => {
      getSocket().receive({
        event_type: "attached",
        session_id: "abc123.main",
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: {},
        payload: {},
      });
    });

    await waitFor(() => {
      expect(currentSessionId).toBe("abc123.main");
    });

    act(() => {
      rerender({ sid: "abc123.main" });
    });

    await waitFor(() => expect(result.current.isPending).toBe(false));

    act(() => {
      result.current.send("hello");
    });

    const sent = findSendMessage();
    expect(sent).toBeDefined();
    expect(sent?.session_id).toBe("abc123.main");
    expect(sent?.content).toBe("hello");
    expect(sent?.ws).toBe("/some/workspace");
  });

  it("does not send ws in send_message when currentWs is undefined", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    let currentSessionId: string | null = pendingUuid;
    const onSessionReady = (uuidPrefix: string, fullSessionId: string): void => {
      expect(uuidPrefix).toBe(pendingUuid);
      currentSessionId = fullSessionId;
    };

    const { result, rerender } = renderHook(
      ({ sid }: { sid: string | null }) =>
        useWebUIStream(sid, getPoolForUuid, onSessionReady),
      { initialProps: { sid: pendingUuid } },
    );

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(findAttach()).toBeDefined());

    act(() => {
      getSocket().receive({
        event_type: "attached",
        session_id: "abc123.main",
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: {},
        payload: {},
      });
    });

    await waitFor(() => {
      expect(currentSessionId).toBe("abc123.main");
    });

    act(() => {
      rerender({ sid: "abc123.main" });
    });

    await waitFor(() => expect(result.current.isPending).toBe(false));

    act(() => {
      result.current.send("hello");
    });

    const sent = findSendMessage();
    expect(sent).toBeDefined();
    expect(sent?.session_id).toBe("abc123.main");
    expect(sent?.content).toBe("hello");
    expect(sent?.ws).toBeUndefined();
  });

  it("attaches when the socket opens after the pending session is selected", async () => {
    // Regression repro for the race where the pending session is selected
    // before the WebSocket finishes opening. The hook must attach once the
    // socket opens.
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    // Delay socket open so selection happens first.
    class DelayedSocket extends FakeWebSocket {
      constructor() {
        super();
        sockets.push(this);
      }
      triggerOpen(): void {
        this.readyState = FakeWebSocket.OPEN;
        this.onopen?.call(this as unknown as WebSocket, new Event("open"));
      }
    }
    vi.stubGlobal("WebSocket", DelayedSocket);

    const { result } = renderHook(() => useWebUIStream(pendingUuid, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    // No attach yet because socket is not open.
    expect(findAttach()).toBeUndefined();

    // Socket finally opens.
    act(() => {
      (getSocket() as DelayedSocket).triggerOpen();
    });

    await waitFor(() => {
      const attach = findAttach();
      expect(attach).toBeDefined();
      expect(attach?.uuid_prefix).toBe(pendingUuid);
    });
  });

  it("notifies onSessionCreated when a subagent conversation is spawned", async () => {
    const onSessionCreated = vi.fn();
    const { result } = renderHook(() =>
      useWebUIStream("abc123.main", undefined, undefined, undefined, undefined, onSessionCreated),
    );

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    act(() => {
      getSocket().receive({
        event_type: "conversation_created",
        session_id: "abc123.main.reviewer.xyz",
        agent_name: "reviewer",
        pool: "coding",
        parent_session_id: "abc123.main",
        metadata: {},
        payload: {},
      });
    });

    await waitFor(() => {
      expect(onSessionCreated).toHaveBeenCalledWith("abc123.main.reviewer.xyz", "abc123.main");
    });
  });

  it("dedups the optimistic user message against the server echo via _request_id", async () => {
    // Regression repro: send() must transmit _request_id so the backend can
    // echo it back on the user_message event (server.py:1061/1137). Without it
    // the reducer cannot match the echo to the optimistic message and renders
    // the user message TWICE (optimistic + un-deduped echo) while the
    // transcript stores only one — exactly the reported symptom.
    vi.mocked(fetchMessages).mockImplementation(() => new Promise(() => {}));

    const fullSid = "abc123.main";
    const getPoolForUuid = (): undefined => undefined; // existing session, not pending

    const { result } = renderHook(() => useWebUIStream(fullSid, getPoolForUuid));

    act(() => {
      result.current.connect();
    });
    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    act(() => {
      result.current.send("hello");
    });

    const sent = findSendMessage();
    expect(sent).toBeDefined();
    // The outgoing frame MUST carry _request_id for the backend to echo it.
    expect(sent?._request_id).toEqual(expect.any(String));

    // Round-trip: backend echoes a user_message with the SAME _request_id
    // (mirroring server.py:1137-1138).
    const requestId = sent?._request_id as string;
    act(() => {
      getSocket().receive({
        event_type: "user_message",
        session_id: fullSid,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: { _request_id: requestId },
        payload: { content: "hello" },
        timestamp: 123,
      });
    });

    // Exactly ONE user message — not the optimistic + the echo.
    const userMsgs = result.current.messages.filter((m) => m.role === "user");
    expect(userMsgs).toHaveLength(1);
    expect(userMsgs[0]?.blocks[0]).toMatchObject({ kind: "text", text: "hello" });
  });
});
