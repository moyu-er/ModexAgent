import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWebUIStream } from "./useWebUIStream";

vi.mock("../lib/api", () => ({
  fetchMessages: vi.fn().mockResolvedValue([]),
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

describe("attach carries ws", () => {
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

  it("sends ws in new-conversation attach", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    const { result } = renderHook(() =>
      useWebUIStream(pendingUuid, getPoolForUuid, undefined, undefined, "/some/workspace"),
    );

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    await waitFor(() => {
      const attach = findAttach();
      expect(attach).toBeDefined();
      expect(attach?.ws).toBe("/some/workspace");
    });
  });

  it("does not send ws when currentWs is undefined", async () => {
    const pendingUuid = "abc123";
    const getPoolForUuid = (uuid: string): string | undefined =>
      uuid === pendingUuid ? "main" : undefined;

    const { result } = renderHook(() => useWebUIStream(pendingUuid, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(sockets.length).toBeGreaterThan(0));

    await waitFor(() => {
      const attach = findAttach();
      expect(attach).toBeDefined();
      expect(attach?.ws).toBeUndefined();
    });
  });
});
