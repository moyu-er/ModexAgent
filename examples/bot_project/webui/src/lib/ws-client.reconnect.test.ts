import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { WebSocketClient } from "./ws-client";

// Minimal fake WebSocket suitable for reconnect timing tests. The client
// stores handlers on the instance it created; we expose them so tests can
// drive open/close transitions and observe new socket creations.
interface FakeSocket {
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  close: () => void;
}

let created: FakeSocket[] = [];

const makeFake = (): FakeSocket => {
  const socket: FakeSocket = {
    readyState: 1, // OPEN
    onopen: null,
    onclose: null,
    onmessage: null,
    onerror: null,
    close: (): void => {
      socket.readyState = 3; // CLOSED
      // Notify asynchronously like a real socket would.
      queueMicrotask(() => {
        socket.onclose?.(new CloseEvent("close"));
      });
    },
  };
  created.push(socket);
  return socket;
};

const installWebSocket = (): void => {
  // The client reads WebSocket.OPEN and the constructor; happy-dom doesn't
  // provide a usable WebSocket, so install a class that hands out fakes and
  // exposes the handlers for the test to drive.
  const FakeCtor = function (): FakeSocket {
    const socket = makeFake();
    // Wire constructor-assigned handlers so the client can set onopen etc.
    return socket;
  } as unknown as { new (): FakeSocket; OPEN: number; CLOSED: number };
  FakeCtor.OPEN = 1;
  FakeCtor.CLOSED = 3;
  vi.stubGlobal("WebSocket", FakeCtor);
};

describe("WebSocketClient reconnect", () => {
  beforeEach(() => {
    created = [];
    installWebSocket();
    vi.useFakeTimers();
    // happy-dom may not provide window.location; set what the client reads.
    vi.stubGlobal("window", {
      location: { protocol: "http:", host: "localhost" },
      ...(globalThis as Record<string, unknown>).window as object,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("reconnects after an unexpected close (backoff schedule)", () => {
    const onOpen = vi.fn();
    const client = new WebSocketClient("/ws", () => {}, undefined, onOpen);

    client.connect();
    // First socket opens synchronously in this fake (constructor sets OPEN).
    expect(created).toHaveLength(1);
    created[0]!.onopen?.(new Event("open"));
    expect(onOpen).toHaveBeenCalledTimes(1);

    // Simulate an UNEXPECTED close (server dropped the socket).
    created[0]!.onclose?.(new CloseEvent("close"));

    // No immediate reconnect — backoff. First attempt at ~1s.
    expect(created).toHaveLength(1);
    vi.advanceTimersByTime(900);
    expect(created).toHaveLength(1);
    vi.advanceTimersByTime(200); // ~1100ms total
    expect(created).toHaveLength(2);

    // The new socket should fire onOpen when it connects.
    created[1]!.onopen?.(new Event("open"));
    expect(onOpen).toHaveBeenCalledTimes(2);

    client.disconnect();
  });

  it("does NOT reconnect after explicit disconnect()", () => {
    const onOpen = vi.fn();
    const client = new WebSocketClient("/ws", () => {}, undefined, onOpen);

    client.connect();
    created[0]!.onopen?.(new Event("open"));
    expect(onOpen).toHaveBeenCalledTimes(1);

    client.disconnect();
    // The client nulls its ws reference directly without invoking the fake's
    // onclose (it calls ws.close() then drops it). Advance well past the max
    // backoff to be sure nothing schedules.
    vi.advanceTimersByTime(120_000);
    expect(created).toHaveLength(1);
    expect(onOpen).toHaveBeenCalledTimes(1);

    // Even if a close event were to fire post-disconnect, the _manualClose
    // guard suppresses reconnect.
    created[0]!.onclose?.(new CloseEvent("close"));
    vi.advanceTimersByTime(120_000);
    expect(created).toHaveLength(1);
  });

  it("stops retrying after the attempt cap", () => {
    const onOpen = vi.fn();
    const client = new WebSocketClient("/ws", () => {}, undefined, onOpen);

    client.connect();
    // Close the initial socket; then every reconnect attempt also fails
    // (immediately closed again) so the backoff sequence runs to exhaustion.
    const driveFailures = (): void => {
      // Advance timers and close each newly-created socket until no new one
      // appears. Each reconnect creates one socket; closing it schedules the
      // next backoff timer.
      let prevCount = 0;
      // eslint-disable-next-line no-constant-condition
      while (true) {
        vi.advanceTimersByTime(35_000); // > max single delay (30s)
        if (created.length === prevCount) break;
        // Close every socket that hasn't been closed yet.
        for (let i = prevCount; i < created.length; i++) {
          created[i]!.onclose?.(new CloseEvent("close"));
        }
        prevCount = created.length;
      }
    };
    created[0]!.onclose?.(new CloseEvent("close"));
    driveFailures();

    // 1 initial + 10 reconnect attempts = 11 sockets total.
    expect(created).toHaveLength(11);

    // Nothing further schedules after the cap.
    vi.advanceTimersByTime(120_000);
    expect(created).toHaveLength(11);

    client.disconnect();
  });
});
