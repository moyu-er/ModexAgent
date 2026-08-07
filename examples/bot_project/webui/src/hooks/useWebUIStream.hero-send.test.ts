import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWebUIStream } from "./useWebUIStream";
import { fetchMessages, uploadAttachment } from "../lib/api";

vi.mock("../lib/api", () => ({
  fetchMessages: vi.fn().mockResolvedValue([]),
  fetchTodos: vi.fn().mockResolvedValue([]),
  fetchApprovals: vi.fn().mockResolvedValue([]),
  submitApproval: vi.fn().mockResolvedValue({ accepted: true }),
  uploadAttachment: vi.fn().mockResolvedValue({
    local_path: "/tmp/uploads/fake.png",
    filename: "fake.png",
    size: 100,
    mime: "image/png",
  }),
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

describe("useWebUIStream hero-send (draft → attached → fullId)", () => {
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

  const findSendMessage = (): Record<string, unknown> | undefined => {
    return getSocket().sent
      .map((s) => JSON.parse(s) as Record<string, unknown>)
      .find((m) => m.action === "send_message");
  };

  it("send() in draft stage adds optimistic message immediately", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    const { result } = renderHook(() => useWebUIStream(pendingUuid, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    act(() => {
      result.current.send("hello from hero");
    });

    const userMsgs = result.current.messages.filter((m) => m.role === "user");
    expect(userMsgs).toHaveLength(1);
    expect(userMsgs[0]?.blocks[0]).toMatchObject({ kind: "text", text: "hello from hero" });
  });

  it("send() in draft stage does NOT transmit ws send_message (queued)", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    const { result } = renderHook(() => useWebUIStream(pendingUuid, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    act(() => {
      result.current.send("hello from hero");
    });

    expect(findSendMessage()).toBeUndefined();
  });

  it("attached event flushes queued ws send with full session id", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    let currentSessionId: string | null = pendingUuid;
    const onSessionReady = (_uuidPrefix: string, fullSessionId: string): void => {
      currentSessionId = fullSessionId;
    };

    const { result } = renderHook(() =>
      useWebUIStream(pendingUuid, getPoolForUuid, onSessionReady),
    );

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    act(() => {
      result.current.send("hero message");
    });

    expect(findSendMessage()).toBeUndefined();

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

    const sent = findSendMessage();
    expect(sent).toBeDefined();
    expect(sent?.session_id).toBe("abc123.main");
    expect(sent?.content).toBe("hero message");
  });

  it("preserves optimistic message when draft promotes to fullId + history loads", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);

    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    let currentSessionId: string | null = pendingUuid;
    const onSessionReady = (_uuidPrefix: string, fullSessionId: string): void => {
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

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    act(() => {
      result.current.send("hero message");
    });

    expect(
      result.current.messages.filter((m) => m.role === "user"),
    ).toHaveLength(1);

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

    await waitFor(() => {
      expect(result.current.isPending).toBe(false);
    });

    await waitFor(() => {
      const userMsgs = result.current.messages.filter((m) => m.role === "user");
      expect(userMsgs.length).toBeGreaterThanOrEqual(1);
      expect(userMsgs[0]?.blocks[0]).toMatchObject({ kind: "text", text: "hero message" });
    });
  });

  it("hero-mode lazy upload: files are uploaded on attached then sent as refs", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    const { result } = renderHook(() =>
      useWebUIStream(pendingUuid, getPoolForUuid),
    );

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    const fakeFile = new File([new Uint8Array(100)], "pic.png", { type: "image/png" });

    act(() => {
      result.current.send("with attachment", undefined, undefined, undefined, [fakeFile]);
    });

    expect(findSendMessage()).toBeUndefined();

    vi.mocked(uploadAttachment).mockClear();
    vi.mocked(uploadAttachment).mockResolvedValue({
      local_path: "/tmp/uploads/abc.png",
      filename: "pic.png",
      size: 100,
      mime: "image/png",
    });

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
      expect(vi.mocked(uploadAttachment)).toHaveBeenCalledWith(
        "abc123.main",
        fakeFile,
        undefined,
      );
    });

    await waitFor(() => {
      const sent = findSendMessage();
      expect(sent).toBeDefined();
      expect(sent?.session_id).toBe("abc123.main");
      expect(sent?.content).toBe("with attachment");
      expect(sent?.attachments).toEqual([
        {
          local_path: "/tmp/uploads/abc.png",
          filename: "pic.png",
          mime: "image/png",
        },
      ]);
    });
  });

  it("hero-mode lazy upload failure drops optimistic message and aborts send", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    const { result } = renderHook(() =>
      useWebUIStream(pendingUuid, getPoolForUuid),
    );

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    const fakeFile = new File([new Uint8Array(100)], "pic.png", { type: "image/png" });

    act(() => {
      result.current.send("with attachment", undefined, undefined, undefined, [fakeFile]);
    });

    vi.mocked(uploadAttachment).mockClear();
    vi.mocked(uploadAttachment).mockRejectedValue(new Error("network error"));

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
      expect(vi.mocked(uploadAttachment)).toHaveBeenCalled();
    });

    await new Promise((r) => setTimeout(r, 50));

    expect(findSendMessage()).toBeUndefined();
    const userMsgs = result.current.messages.filter((m) => m.role === "user");
    expect(userMsgs).toHaveLength(0);
  });
});
