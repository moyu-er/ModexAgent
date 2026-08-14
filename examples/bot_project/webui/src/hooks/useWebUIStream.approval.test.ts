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
    expect(fetchApprovals).toHaveBeenCalledWith(sessionId, undefined, undefined);
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

    await waitFor(() => expect(fetchApprovals).toHaveBeenCalledWith(sessionId, undefined, undefined));
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

    await waitFor(() => expect(fetchApprovals).toHaveBeenCalledWith(sessionId, workspace, undefined));
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

    expect(submitApproval).toHaveBeenCalledWith(sessionId, "tc_1", "allow", undefined, undefined);
    expect(result.current.pendingApprovals).toEqual([]);
    // Batch lock resets to false after completion (was true while in flight).
    expect(result.current.isApprovingBatch).toBe(false);
  });

  it("re-fetches the authoritative pending list when approval_request arrives via WebSocket", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    // Initial load: empty.
    vi.mocked(fetchApprovals).mockResolvedValue([]);

    const sessionId = "apr001.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.pendingApprovals).toEqual([]));

    // The backend emits ONE approval_request on suspend; the GET endpoint
    // returns the full pending set.
    vi.mocked(fetchApprovals).mockResolvedValue([
      {
        tool_call_id: "tc_a",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "ls" },
        status: "pending",
      },
      {
        tool_call_id: "tc_b",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "rm" },
        status: "pending",
      },
    ]);

    act(() => {
      const socket = sockets[sockets.length - 1]!;
      socket.receive({
        event_type: "approval_request",
        session_id: sessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: {},
        payload: {
          tool_call_id: "tc_a",
          tool_name: "shell",
          tier: "high",
          arguments: { cmd: "ls" },
          status: "pending",
        },
      });
    });

    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(2));
    // The single push (tc_a) was corrected to the full pending set pulled
    // from the GET endpoint (tc_a + tc_b).
    expect(result.current.pendingApprovals.map((v) => v.tool_call_id)).toEqual([
      "tc_a",
      "tc_b",
    ]);
    expect(fetchApprovals).toHaveBeenCalledWith(sessionId, undefined, undefined);
  });

  it("clears isStreaming for its session when approval_request arrives", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    vi.mocked(fetchApprovals).mockResolvedValue([]);

    const sessionId = "apr002.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(fetchApprovals).toHaveBeenCalledWith(sessionId, undefined, undefined));

    // Simulate a streaming turn in progress, then a suspend-for-approval.
    act(() => {
      const socket = sockets[sockets.length - 1]!;
      socket.receive({
        event_type: "model_content_delta",
        session_id: sessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: { turn_id: "t1" },
        payload: { text: "thinking...", turn_id: "t1" },
      });
    });

    await waitFor(() => expect(result.current.isStreaming).toBe(true));

    act(() => {
      const socket = sockets[sockets.length - 1]!;
      socket.receive({
        event_type: "approval_request",
        session_id: sessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: {},
        payload: {
          tool_call_id: "tc_a",
          tool_name: "shell",
          tier: "high",
          arguments: { cmd: "ls" },
          status: "pending",
        },
      });
    });

    // Suspend never emits turn_end; approval_request must clear the busy
    // flag so the composer is no longer "streaming".
    await waitFor(() => expect(result.current.isStreaming).toBe(false));
  });

  it("exposes isApprovingBatch true while a submit is in flight, false after it resolves", async () => {
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

    // Hold the submit POST pending so we can observe the in-flight lock.
    let resolveSubmit!: (value: { accepted: boolean }) => void;
    vi.mocked(submitApproval).mockImplementation(
      () => new Promise((resolve) => { resolveSubmit = resolve; }),
    );

    const sessionId = "apr003.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(1));

    expect(result.current.isApprovingBatch).toBe(false);

    act(() => {
      result.current.submitApproval("tc_1", "allow");
    });

    // While the POST is in flight, the batch lock is on.
    await waitFor(() => expect(result.current.isApprovingBatch).toBe(true));

    await act(async () => {
      resolveSubmit({ accepted: true });
    });

    // After the POST resolves, the lock releases.
    expect(result.current.isApprovingBatch).toBe(false);
  });

  it("ignores an approval_request-triggered fetch while a decision POST is in flight (no phantom card)", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    // Initial load: one pending approval the user is about to decide.
    vi.mocked(fetchApprovals).mockResolvedValue([
      {
        tool_call_id: "tc_1",
        tool_name: "shell",
        tier: "high",
        arguments: { cmd: "ls" },
        status: "pending",
      },
    ]);

    // Hold the submit POST unresolved: the in-flight flag stays set and
    // the optimistic clear (which runs in the POST's .then) has NOT yet
    // fired, so tc_1 remains in the pending list while it is flagged
    // in-flight. This is the canonical stale-fetch race window.
    let resolveSubmit!: (value: { accepted: boolean }) => void;
    vi.mocked(submitApproval).mockImplementation(
      () => new Promise((resolve) => { resolveSubmit = resolve; }),
    );

    const sessionId = "apr004.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(1));

    // User clicks Approve → in-flight flag set; card is NOT yet cleared
    // (clear runs on POST resolve, which we are holding open).
    act(() => {
      result.current.submitApproval("tc_1", "allow");
    });

    await waitFor(() => {
      expect(result.current.isApprovingBatch).toBe(true);
      expect(result.current.pendingApprovals.map((v) => v.tool_call_id)).toEqual(["tc_1"]);
    });

    // Stash the in-flight count before the approval_request fires. A
    // second, later approval_request fetch returns a stale list that STILL
    // includes tc_1 (captured before the backend recorded the decision).
    const callsBefore = vi.mocked(fetchApprovals).mock.calls.length;

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
        event_type: "approval_request",
        session_id: sessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: {},
        payload: {
          tool_call_id: "tc_1",
          tool_name: "shell",
          tier: "high",
          arguments: { cmd: "ls" },
          status: "pending",
        },
      });
    });

    // Wait for the approval_request-triggered fetch to be issued + settle.
    await waitFor(() => expect(vi.mocked(fetchApprovals).mock.calls.length).toBe(callsBefore + 1));
    // Flush the fetch's .then chain so a setState would have run if the
    // guard were absent.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    // The in-flight guard must win: the stale fetch result (tc_1 + tc_2)
    // is ignored, and the pending list is unchanged from before the fetch —
    // only tc_1 (still legitimately pending, awaiting POST resolution), no
    // phantom tc_2 and no stale re-ordering.
    expect(result.current.pendingApprovals.map((v) => v.tool_call_id)).toEqual(["tc_1"]);
    expect(result.current.isApprovingBatch).toBe(true);

    // Once the submit resolves, the optimistic clear fires and the next
    // approval_request reconciles to the authoritative view.
    vi.mocked(fetchApprovals).mockResolvedValue([]);
    await act(async () => {
      resolveSubmit({ accepted: true });
    });
    await waitFor(() => expect(result.current.pendingApprovals).toEqual([]));
  });

  it("Approve-All loop drains all pending cards to empty and releases the batch lock", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    // Seed three pending approvals (A, B, C) — mirrors the multi-card
    // Approve-All scenario driven in production by App.onApproveAll, which
    // calls submitApproval(id, "allow") for each pending card.
    const seed = ["tc_A", "tc_B", "tc_C"].map((id) => ({
      tool_call_id: id,
      tool_name: "shell",
      tier: "high" as const,
      arguments: { cmd: "ls" },
      status: "pending" as const,
    }));
    vi.mocked(fetchApprovals).mockResolvedValue(seed);
    // Earlier tests in this file override submitApproval with a held Promise;
    // clearAllMocks (in beforeEach) does not reset the implementation, so
    // restore the default immediate-resolution mock for this test.
    vi.mocked(submitApproval).mockResolvedValue({ accepted: true });

    const sessionId = "apr005.main";
    const getPoolForUuid = (): undefined => undefined;

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.pendingApprovals).toHaveLength(3));

    // Drive the Approve-All semantics directly: submit each card. The hook's
    // submitApproval returns void (fire-and-forget the POST), so wrap each
    // call in its own async act to flush the mocked POST's .then(clear) and
    // .finally(release flag) before the next card is submitted — matching the
    // sequential resolution the production loop observes.
    await act(async () => { await result.current.submitApproval("tc_A", "allow"); });
    await act(async () => { await result.current.submitApproval("tc_B", "allow"); });
    await act(async () => { await result.current.submitApproval("tc_C", "allow"); });

    expect(submitApproval).toHaveBeenCalledWith(sessionId, "tc_A", "allow", undefined, undefined);
    expect(submitApproval).toHaveBeenCalledWith(sessionId, "tc_B", "allow", undefined, undefined);
    expect(submitApproval).toHaveBeenCalledWith(sessionId, "tc_C", "allow", undefined, undefined);

    // Terminal state: all cards cleared, batch lock released.
    expect(result.current.pendingApprovals).toEqual([]);
    expect(result.current.isApprovingBatch).toBe(false);
  });

  it("does not re-append an approval_request for a card whose decision POST is in flight (phantom suppression)", async () => {
    vi.mocked(fetchMessages).mockResolvedValue([]);
    vi.mocked(fetchTodos).mockResolvedValue([]);
    // Initial load: the card under decision is NOT in the pending list — it
    // was just optimistically cleared. The reducer's dedupe checks the
    // currently-present ids, so without the hook guard it would re-append.
    vi.mocked(fetchApprovals).mockResolvedValue([]);

    // Hold the submit POST unresolved so the in-flight flag stays set across
    // the stale approval_request arrival.
    let resolveSubmit!: (value: { accepted: boolean }) => void;
    vi.mocked(submitApproval).mockImplementation(
      () => new Promise((resolve) => { resolveSubmit = resolve; }),
    );

    const sessionId = "apr006.main";
    const getPoolForUuid = (): undefined => undefined;
    const phantomTc = "tc_x";

    const { result } = renderHook(() => useWebUIStream(sessionId, getPoolForUuid));

    act(() => {
      result.current.connect();
    });

    await waitFor(() => expect(result.current.pendingApprovals).toEqual([]));

    // User decided a card (e.g. from an earlier render) — POST in flight.
    act(() => {
      result.current.submitApproval(phantomTc, "allow");
    });
    await waitFor(() => expect(result.current.isApprovingBatch).toBe(true));

    // A stale approval_request for the in-flight card arrives. The pending
    // list does NOT contain it, so the reducer's dedupe would pass and it
    // would be appended — the hook guard must suppress the append.
    act(() => {
      const socket = sockets[sockets.length - 1]!;
      socket.receive({
        event_type: "approval_request",
        session_id: sessionId,
        agent_name: "main",
        pool: "main",
        parent_session_id: null,
        metadata: {},
        payload: {
          tool_call_id: phantomTc,
          tool_name: "shell",
          tier: "high",
          arguments: { cmd: "ls" },
          status: "pending",
        },
      });
    });
    // Flush any pending microtasks so an unguarded setState would have run.
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    // Phantom suppressed: card NOT re-appended.
    expect(result.current.pendingApprovals.map((v) => v.tool_call_id)).toEqual([]);

    // Resolve the POST; state stays clean.
    vi.mocked(fetchApprovals).mockResolvedValue([]);
    await act(async () => {
      resolveSubmit({ accepted: true });
    });
    await waitFor(() => expect(result.current.isApprovingBatch).toBe(false));
    expect(result.current.pendingApprovals).toEqual([]);
  });
});
