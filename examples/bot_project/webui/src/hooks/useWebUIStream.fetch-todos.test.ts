import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useWebUIStream } from "./useWebUIStream";
import { fetchMessages, fetchTodos } from "../lib/api";

vi.mock("../lib/api", () => ({
  fetchMessages: vi.fn().mockResolvedValue([]),
  fetchTodos: vi.fn().mockResolvedValue([]),
  fetchApprovals: vi.fn().mockResolvedValue([]),
  submitApproval: vi.fn().mockResolvedValue({ accepted: true }),
}));

// Minimal fake WebSocket that opens immediately.
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

describe("useWebUIStream todo fetch", () => {
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

  it("loads todos from the dedicated endpoint when a session is selected", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([
      { content: "current", status: "in_progress" },
      { content: "next", status: "pending" },
    ]);

    const sessionId = "abc123.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.todos.length).toBe(2));
    expect(result.current.todos).toEqual([
      { content: "current", status: "in_progress" },
      { content: "next", status: "pending" },
    ]);
    expect(fetchTodos).toHaveBeenCalledWith(sessionId, undefined, undefined);
  });

  it("returns empty array when session has no todos", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);

    const sessionId = "abc123.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    // fetchTodos resolved to [] — the hook should expose an empty list.
    // TodoPanel renders null when todos.length === 0, so no extra UI appears.
    await waitFor(() => expect(result.current.todos).toEqual([]));
  });

  it("passes currentWs to the todo endpoint", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);

    const sessionId = "abc123.main";
    const getPoolForUuid = (): undefined => undefined;
    const workspace = "/some/workspace";

    renderHook(() => useWebUIStream(sessionId, getPoolForUuid, undefined, undefined, workspace));

    await waitFor(() => expect(fetchTodos).toHaveBeenCalledWith(sessionId, workspace, undefined));
  });

  it("re-fetches todos when a todo_write tool_call_end arrives via WebSocket", async () => {
    // Initial fetch returns two items.
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([
      { content: "a", status: "in_progress" },
      { content: "b", status: "pending" },
    ]);

    const sessionId = "abc123.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.todos).toHaveLength(2));

    // Now the agent calls todo_write — re-fetch returns a revised list.
    vi.mocked(fetchTodos).mockResolvedValue([
      { content: "a", status: "completed" },
      { content: "b", status: "in_progress" },
      { content: "c", status: "pending" },
    ]);

    act(() => {
      const socket = sockets[sockets.length - 1]!;
      socket.receive({
        event_type: "tool_call_end",
        session_id: sessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: { turn_id: "t1" },
        payload: {
          tool: "todo_write",
          result_summary: "truncated...",
          turn_id: "t1",
        },
      });
    });

    await waitFor(() => expect(result.current.todos).toHaveLength(3));
    expect(result.current.todos).toEqual([
      { content: "a", status: "completed" },
      { content: "b", status: "in_progress" },
      { content: "c", status: "pending" },
    ]);
    // fetchTodos should have been called twice: once on session load, once on tool_call_end.
    expect(fetchTodos).toHaveBeenCalledTimes(2);
  });
});
