import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWebUIStream } from "./useWebUIStream";
import { fetchApprovals, fetchMessages, fetchTodos, submitApproval } from "../lib/api";

vi.mock("../lib/api", () => ({
  fetchMessages: vi.fn().mockResolvedValue([]),
  fetchTodos: vi.fn().mockResolvedValue([]),
  fetchApprovals: vi.fn().mockResolvedValue([]),
  submitApproval: vi.fn().mockResolvedValue({ accepted: true }),
}));

// Minimal fake WebSocket that opens immediately. Mirrors
// useWebUIStream.fetch-todos.test.ts harness.
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

describe("useWebUIStream approval fetch/render/submit", () => {
  beforeEach(() => {
    sockets = [];
    vi.clearAllMocks();
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

  it("loads pending approvals from the dedicated endpoint when a session is selected", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    vi.mocked(fetchApprovals).mockResolvedValue([
      {
        tool_call_id: "tc_1",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "rm -rf /" },
        status: "pending",
      },
    ]);

    const sessionId = "abc123.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(1));
    expect(result.current.pendingApprovals).toEqual([
      {
        tool_call_id: "tc_1",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "rm -rf /" },
        status: "pending",
      },
    ]);
    expect(fetchApprovals).toHaveBeenCalledWith(sessionId, undefined);
  });

  it("exposes an empty list (not undefined) for the selected session", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    vi.mocked(fetchApprovals).mockResolvedValue([]);

    const sessionId = "abc456.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(fetchApprovals).toHaveBeenCalledWith(sessionId, undefined));
    expect(result.current.pendingApprovals).toEqual([]);
  });

  it("passes currentWs to the approvals endpoint", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    vi.mocked(fetchApprovals).mockResolvedValue([]);

    const sessionId = "abc789.main";
    const getPoolForUuid = (): undefined => undefined;
    const workspace = "/some/workspace";

    renderHook(() =>
      useWebUIStream(sessionId, getPoolForUuid, undefined, undefined, workspace),
    );

    await waitFor(() => expect(fetchApprovals).toHaveBeenCalledWith(sessionId, workspace));
  });

  it("re-fetches approvals when turn_end arrives via WebSocket", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    // Initial fetch: one pending approval.
    vi.mocked(fetchApprovals).mockResolvedValue([
      {
        tool_call_id: "tc_1",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "ls" },
        status: "pending",
      },
    ]);

    const sessionId = "def123.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(1));

    // After turn_end, the freshly-suspended approval appears.
    vi.mocked(fetchApprovals).mockResolvedValue([
      {
        tool_call_id: "tc_1",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "ls" },
        status: "pending",
      },
      {
        tool_call_id: "tc_2",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "rm" },
        status: "pending",
      },
    ]);

    act(() => {
      const socket = sockets[sockets.length - 1]!;
      socket.receive({
        event_type: "turn_end",
        session_id: sessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: { turn_id: "t1" },
        payload: { turn_id: "t1" },
      });
    });

    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(2));
    // fetchApprovals called twice: once on load, once on turn_end.
    expect(fetchApprovals).toHaveBeenCalledTimes(2);
  });

  it("submitApproval POSTs and clears the approval on success", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    vi.mocked(fetchApprovals).mockResolvedValue([
      {
        tool_call_id: "tc_1",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "ls" },
        status: "pending",
      },
    ]);
    vi.mocked(submitApproval).mockResolvedValue({ accepted: true });

    const sessionId = "ghi123.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(1));

    await act(async () => {
      await result.current.submitApproval("tc_1", "allow");
    });

    expect(submitApproval).toHaveBeenCalledWith(sessionId, "tc_1", "allow", undefined);
    expect(result.current.pendingApprovals).toEqual([]);
    // submitting flag resets to false after completion.
    expect(result.current.submittingApprovals["tc_1"]).toBeUndefined();
  });
});
