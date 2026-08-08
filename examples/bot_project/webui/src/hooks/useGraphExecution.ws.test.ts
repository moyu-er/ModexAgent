// useGraphExecution.ws.test.ts — WS mode tests for G11 (PRD §11.2 Phase 2).
//
// Tests the WS data path: subscribe/unsubscribe lifecycle, event-driven
// node status / precise deliver pulses / real-timestamp timeline, pulse
// dedup (node_completed vs deliver_dispatched), and WS-disconnect →
// polling fallback + reconnect re-subscribe.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useGraphExecution } from "./useGraphExecution";
import { getInstance, getEvents } from "../lib/graphsApi";
import type { GraphInstance, GraphNodeStatus } from "../lib/graphsApi";
import type {
  GraphOutputEvent,
  GraphWsMessage,
  WebSocketClient,
} from "../lib/ws-client";
import type { GraphTopologyEdge } from "./useGraphExecution.diff";

vi.mock("../lib/graphsApi", () => ({
  getInstance: vi.fn(),
  getEvents: vi.fn(),
}));

const mockGetInstance = vi.mocked(getInstance);
const mockGetEvents = vi.mocked(getEvents);

// ── Fixtures ─────────────────────────────────────────────────────────────────

function node(
  name: string,
  status: string,
  id = `${name}-id`,
): GraphNodeStatus {
  return { node_name: name, node_id: id, status };
}

function instance(
  status: string,
  nodes: GraphNodeStatus[],
): GraphInstance {
  return {
    spec_id: "spec-1",
    graph_instance_id: "inst-1",
    status,
    nodes,
    result: null,
  };
}

const EDGES: GraphTopologyEdge[] = [
  { source: "a", target: "b" },
  { source: "b", target: "c" },
];

const BASE_INSTANCE = instance("running", [
  node("a", "pending"),
  node("b", "pending"),
  node("c", "pending"),
]);

// ── Fake WebSocketClient ─────────────────────────────────────────────────────

/**
 * Minimal stub implementing the WebSocketClient surface the hook uses:
 * connected, setGraphHandler, addConnectionListener, removeConnectionListener,
 * send. Test helpers (simulateConnect/Disconnect, injectGraphEvent) let tests
 * drive the WS state machine.
 */
class FakeWsClient {
  connected: boolean;
  private graphHandler: ((msg: GraphWsMessage) => void) | null = null;
  private connectionListeners: Set<(connected: boolean) => void> = new Set();
  readonly sent: { type: string; payload: Record<string, unknown> }[] = [];

  constructor(connected = true) {
    this.connected = connected;
  }

  setGraphHandler(handler: ((msg: GraphWsMessage) => void) | null): void {
    this.graphHandler = handler;
  }

  addConnectionListener(listener: (connected: boolean) => void): void {
    this.connectionListeners.add(listener);
  }

  removeConnectionListener(listener: (connected: boolean) => void): void {
    this.connectionListeners.delete(listener);
  }

  send(type: string, payload: Record<string, unknown>): boolean {
    this.sent.push({ type, payload });
    return true;
  }

  // ── Test helpers ──────────────────────────────────────────────────────────

  simulateConnect(): void {
    this.connected = true;
    this.connectionListeners.forEach((l) => l(true));
  }

  simulateDisconnect(): void {
    this.connected = false;
    this.connectionListeners.forEach((l) => l(false));
  }

  injectGraphEvent(event: GraphOutputEvent): void {
    this.graphHandler?.({
      type: "graph_event",
      graph_instance_id: "inst-1",
      event,
    });
  }

  injectGraphError(message: string): void {
    this.graphHandler?.({ type: "graph_error", message });
  }

  /** Inject a raw GraphWsMessage (for testing instance_id filtering). */
  injectRawMessage(msg: GraphWsMessage): void {
    this.graphHandler?.(msg);
  }

  /** Find the first sent message of a given type. */
  sentOfType(type: string): { type: string; payload: Record<string, unknown> } | undefined {
    return this.sent.find((s) => s.type === type);
  }

  /** Count sent messages of a given type. */
  sentCount(type: string): number {
    return this.sent.filter((s) => s.type === type).length;
  }
}

function asWsClient(fake: FakeWsClient): WebSocketClient {
  return fake as unknown as WebSocketClient;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

async function tick(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Flush microtasks without advancing fake timers (for async state updates). */
async function flush(): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0);
  });
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe("useGraphExecution — WS mode", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockGetInstance.mockReset();
    mockGetEvents.mockReset();
    mockGetEvents.mockResolvedValue([]);
    mockGetInstance.mockResolvedValue(BASE_INSTANCE);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // ── Subscribe / unsubscribe lifecycle ─────────────────────────────────────

  it("sends subscribe_graph on mount when WS is already connected", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    const sub = fake.sentOfType("subscribe_graph");
    expect(sub).toBeDefined();
    expect(sub?.payload.instance_id).toBe("inst-1");
    expect(sub?.payload.ws).toBe("ws1");
  });

  it("omits ws from subscribe payload when workspaceId is empty", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    renderHook(() =>
      useGraphExecution("", "inst-1", EDGES, ws),
    );
    await flush();

    const sub = fake.sentOfType("subscribe_graph");
    expect(sub?.payload.ws).toBeUndefined();
  });

  it("sends unsubscribe_graph on unmount", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { unmount } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    unmount();
    await flush();

    expect(fake.sentOfType("unsubscribe_graph")).toBeDefined();
    expect(fake.sentOfType("unsubscribe_graph")?.payload.instance_id).toBe("inst-1");
  });

  it("does not poll while WS is connected (only initial fetch)", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    // One initial getInstance (from polling effect before wsConnected flips).
    const initialCount = mockGetInstance.mock.calls.length;
    expect(initialCount).toBeGreaterThanOrEqual(1);

    await tick(10_000);
    // No additional polls — WS is connected.
    expect(mockGetInstance.mock.calls.length).toBe(initialCount);
  });

  // ── Event-driven node status ──────────────────────────────────────────────

  it("node_started → node status = running + timeline event (derived: false)", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { result } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    expect(result.current.instance?.nodes[0]?.status).toBe("pending");

    act(() => {
      fake.injectGraphEvent({
        kind: "node_started",
        node_id: "a-id",
        node_name: "a",
        invocation_id: 1,
        timestamp: 1_700_000_000_000,
      });
    });

    expect(result.current.instance?.nodes[0]?.status).toBe("running");
    const tl = result.current.timeline.find(
      (e) => e.kind === "node_started",
    );
    expect(tl).toBeDefined();
    expect(tl?.derived).toBe(false);
    expect(tl?.timestamp).toBe(1_700_000_000_000);
    expect(tl?.nodeId).toBe("a-id");
  });

  it("node_completed → node status = completed + out-edge pulse + timeline", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { result } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    act(() => {
      fake.injectGraphEvent({
        kind: "node_completed",
        node_id: "a-id",
        node_name: "a",
        invocation_id: 1,
        timestamp: 1_700_000_000_100,
      });
    });

    expect(result.current.instance?.nodes[0]?.status).toBe("completed");
    // Out-edge: a→b
    expect(result.current.pulses).toHaveLength(1);
    expect(result.current.pulses[0]?.edge).toEqual({ source: "a", target: "b" });
    // Timeline
    const tl = result.current.timeline.find(
      (e) => e.kind === "node_completed",
    );
    expect(tl).toBeDefined();
    expect(tl?.derived).toBe(false);
    expect(tl?.timestamp).toBe(1_700_000_000_100);
  });

  it("node_crashed → node status = crashed + crash flash + timeline", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { result } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    act(() => {
      fake.injectGraphEvent({
        kind: "node_crashed",
        node_id: "a-id",
        node_name: "a",
        timestamp: 1_700_000_000_200,
      });
    });

    expect(result.current.instance?.nodes[0]?.status).toBe("crashed");
    expect(result.current.crashFlashes).toHaveLength(1);
    expect(result.current.crashFlashes[0]?.nodeName).toBe("a");
    expect(result.current.crashFlashes[0]?.timestamp).toBe(1_700_000_000_200);
  });

  // ── Precise deliver pulse ─────────────────────────────────────────────────

  it("deliver_dispatched → precise pulse on source→target edge", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { result } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    act(() => {
      fake.injectGraphEvent({
        kind: "deliver_dispatched",
        node_id: "a-id",
        node_name: "a",
        target_node_id: "b-id",
        timestamp: 1_700_000_000_300,
      });
    });

    expect(result.current.pulses).toHaveLength(1);
    expect(result.current.pulses[0]?.edge).toEqual({ source: "a", target: "b" });
    expect(result.current.pulses[0]?.timestamp).toBe(1_700_000_000_300);
  });

  // ── Pulse dedup ───────────────────────────────────────────────────────────

  it("dedupes: node_completed + deliver_dispatched on same edge → one pulse", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { result } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    // node_completed fires the a→b pulse.
    act(() => {
      fake.injectGraphEvent({
        kind: "node_completed",
        node_id: "a-id",
        node_name: "a",
        timestamp: 1_700_000_000_100,
      });
    });
    expect(result.current.pulses).toHaveLength(1);

    // deliver_dispatched on the same edge within dedup window → skipped.
    act(() => {
      fake.injectGraphEvent({
        kind: "deliver_dispatched",
        node_id: "a-id",
        node_name: "a",
        target_node_id: "b-id",
        timestamp: 1_700_000_000_150,
      });
    });
    expect(result.current.pulses).toHaveLength(1); // no duplicate

    // A deliver_dispatched on a DIFFERENT edge still fires.
    act(() => {
      fake.injectGraphEvent({
        kind: "deliver_dispatched",
        node_id: "b-id",
        node_name: "b",
        target_node_id: "c-id",
        timestamp: 1_700_000_000_200,
      });
    });
    expect(result.current.pulses).toHaveLength(2);
    expect(result.current.pulses[1]?.edge).toEqual({ source: "b", target: "c" });
  });

  // ── Graph-level events ────────────────────────────────────────────────────

  it("graph_completed → instance status = completed + timeline event", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { result } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    act(() => {
      fake.injectGraphEvent({
        kind: "graph_completed",
        timestamp: 1_700_000_000_500,
      });
    });

    expect(result.current.instance?.status).toBe("completed");
    expect(
      result.current.timeline.find((e) => e.kind === "graph_completed"),
    ).toBeDefined();
  });

  it("graph_error → sets error state", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { result } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    act(() => {
      fake.injectGraphError("subscription failed");
    });

    expect(result.current.error).toBe("subscription failed");
  });

  // ── Instance ID filtering ─────────────────────────────────────────────────

  it("ignores graph_event for a different instance_id", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    const { result } = renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    // Inject an event for a different instance_id.
    act(() => {
      fake.injectRawMessage({
        type: "graph_event",
        graph_instance_id: "inst-OTHER",
        event: { kind: "node_started", node_id: "a-id", node_name: "a", timestamp: 100 },
      });
    });

    // No state change.
    expect(result.current.instance?.nodes[0]?.status).toBe("pending");
    expect(result.current.timeline).toEqual([]);
  });

  // ── Disconnect → polling fallback ─────────────────────────────────────────

  it("falls back to polling when WS disconnects", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    const countBefore = mockGetInstance.mock.calls.length;

    // Simulate WS disconnect.
    act(() => fake.simulateDisconnect());
    await flush();

    // Polling should start — getInstance called for the initial fallback poll.
    await tick(1);
    expect(mockGetInstance.mock.calls.length).toBeGreaterThan(countBefore);

    // Continued polling at 2s intervals.
    const countAfterInitial = mockGetInstance.mock.calls.length;
    await tick(2000);
    expect(mockGetInstance.mock.calls.length).toBeGreaterThan(countAfterInitial);
  });

  // ── Reconnect → re-subscribe + stop polling ───────────────────────────────

  it("re-subscribes and stops polling on reconnect", async () => {
    const fake = new FakeWsClient(true);
    const ws = asWsClient(fake);
    renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES, ws),
    );
    await flush();

    // Disconnect → polling starts.
    act(() => fake.simulateDisconnect());
    await tick(2000);
    expect(fake.sentCount("subscribe_graph")).toBe(1); // initial subscribe

    // Reconnect → re-subscribe + stop polling.
    act(() => fake.simulateConnect());
    await flush();

    expect(fake.sentCount("subscribe_graph")).toBe(2); // re-subscribed

    const countAfterReconnect = mockGetInstance.mock.calls.length;
    await tick(10_000);
    // No additional polling — WS is back.
    expect(mockGetInstance.mock.calls.length).toBe(countAfterReconnect);
  });

  // ── Backward compat (no wsClient) ─────────────────────────────────────────

  it("falls back to polling mode when wsClient is not provided", async () => {
    mockGetInstance.mockResolvedValue(instance("running", [node("a", "running")]));
    renderHook(() =>
      useGraphExecution("ws1", "inst-1", EDGES),
    );

    await tick(1);
    expect(mockGetInstance).toHaveBeenCalledTimes(1);

    await tick(2000);
    expect(mockGetInstance).toHaveBeenCalledTimes(2);

    await tick(2000);
    expect(mockGetInstance).toHaveBeenCalledTimes(3);
  });
});
