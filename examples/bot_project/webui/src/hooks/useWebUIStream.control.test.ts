// useWebUIStream — control-channel wiring (PA-02).
//
// Contract: when the host passes onSessionsChanged, the hook registers a
// control handler on its WebSocketClient; a sessions_changed message for the
// pod's workspace calls the callback and never enters the chat reducer (no
// transcript mutation). Chat events keep flowing through onEvent as before.

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWebUIStream } from "./useWebUIStream";
import { fetchMessages, fetchTodos, fetchApprovals } from "../lib/api";
import { WebSocketClient } from "../lib/ws-client";

vi.mock("../lib/api", () => ({
  fetchMessages: vi.fn().mockResolvedValue([]),
  fetchTodos: vi.fn().mockResolvedValue([]),
  fetchApprovals: vi.fn().mockResolvedValue([]),
  fetchSessions: vi.fn(),
  submitApproval: vi.fn(),
  uploadAttachment: vi.fn(),
}));

class FakeWebSocket {
  static readonly OPEN = 1;
  readyState = FakeWebSocket.OPEN;
  onopen: ((ev: Event) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  sent: unknown[] = [];
  constructor() {
    queueMicrotask(() => {
      this.onopen?.(new Event("open"));
    });
  }
  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }
  close(): void {
    this.readyState = 3;
  }
  dispatchEvent(): boolean {
    return true;
  }
}

let sockets: FakeWebSocket[] = [];

describe("useWebUIStream control channel (PA-02)", () => {
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
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    vi.mocked(fetchApprovals).mockResolvedValue([]);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  it("routes sessions_changed to onSessionsChanged, not the chat reducer", async () => {
    const onSessionsChanged = vi.fn();
    const { result } = renderHook(() =>
      useWebUIStream(
        "abc.main",
        undefined,
        undefined,
        undefined,
        "",
        undefined,
        "main",
        onSessionsChanged,
      ),
    );
    act(() => {
      result.current.connect();
    });
    await waitFor(() => expect(result.current.isConnected).toBe(true));
    expect(sockets[0]).toBeTruthy();

    const before = result.current.messages.length;
    act(() => {
      sockets[0]!.onmessage?.({
        data: JSON.stringify({ type: "sessions_changed", workspace: "" }),
      } as MessageEvent);
    });

    expect(onSessionsChanged).toHaveBeenCalledTimes(1);
    expect(onSessionsChanged).toHaveBeenCalledWith("");
    // Not a chat event — the message list is untouched.
    expect(result.current.messages.length).toBe(before);
  });

  it("filters cross-workspace pings before calling the callback", async () => {
    const onSessionsChanged = vi.fn();
    const { result } = renderHook(() =>
      useWebUIStream(
        "abc.main",
        undefined,
        undefined,
        undefined,
        "/ws/a",
        undefined,
        "main",
        onSessionsChanged,
      ),
    );
    act(() => {
      result.current.connect();
    });
    await waitFor(() => expect(result.current.isConnected).toBe(true));

    act(() => {
      sockets[0]!.onmessage?.({
        data: JSON.stringify({ type: "sessions_changed", workspace: "/ws/b" }),
      } as MessageEvent);
      sockets[0]!.onmessage?.({
        data: JSON.stringify({ type: "sessions_changed", workspace: "/ws/a" }),
      } as MessageEvent);
    });

    expect(onSessionsChanged).toHaveBeenCalledTimes(1);
    expect(onSessionsChanged).toHaveBeenCalledWith("/ws/a");
  });

  it("accepts a representation-drift ping for the same Windows workspace (case/separators/trailing), ignores a different one", async () => {
    // The server broadcasts the resolved canonical form; the pod's ws is the
    // tab path. Drift in drive-letter case, separators, or a trailing
    // separator must still match; a genuinely different workspace must not.
    const onSessionsChanged = vi.fn();
    const { result } = renderHook(() =>
      useWebUIStream(
        "abc.main",
        undefined,
        undefined,
        undefined,
        "F:/Tool/Proj",
        undefined,
        "main",
        onSessionsChanged,
      ),
    );
    act(() => {
      result.current.connect();
    });
    await waitFor(() => expect(result.current.isConnected).toBe(true));

    act(() => {
      sockets[0]!.onmessage?.({
        data: JSON.stringify({ type: "sessions_changed", workspace: "f:\\tool\\proj\\" }),
      } as MessageEvent);
      sockets[0]!.onmessage?.({
        data: JSON.stringify({ type: "sessions_changed", workspace: "F:/Tool/Other" }),
      } as MessageEvent);
    });

    expect(onSessionsChanged).toHaveBeenCalledTimes(1);
    expect(onSessionsChanged).toHaveBeenCalledWith("f:\\tool\\proj\\");
  });

  it("keeps chat events flowing (envelope unwrap still works)", async () => {
    const onSessionsChanged = vi.fn();
    const { result } = renderHook(() =>
      useWebUIStream(
        "abc.main",
        undefined,
        undefined,
        undefined,
        "",
        undefined,
        "main",
        onSessionsChanged,
      ),
    );
    act(() => {
      result.current.connect();
    });
    await waitFor(() => expect(result.current.isConnected).toBe(true));

    act(() => {
      sockets[0]!.onmessage?.({
        data: JSON.stringify({
          session_id: "abc.main",
          agent_name: "main",
          event_type: "user_message",
          pool: "main",
          parent_session_id: null,
          metadata: {},
          payload: { content: "hello" },
        }),
      } as MessageEvent);
    });

    expect(onSessionsChanged).not.toHaveBeenCalled();
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0]?.role).toBe("user");
  });

  it("still delivers messages through the WebSocketClient class contract", () => {
    // Sanity: the hook's connect() builds a real WebSocketClient — the
    // control handler registration goes through setControlHandler.
    const spy = vi.spyOn(WebSocketClient.prototype, "setControlHandler");
    const { result, unmount } = renderHook(() =>
      useWebUIStream("abc.main", undefined, undefined, undefined, "", undefined, "main", vi.fn()),
    );
    act(() => {
      result.current.connect();
    });
    expect(spy).toHaveBeenCalled();
    unmount();
    spy.mockRestore();
  });
});
