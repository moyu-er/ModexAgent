import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { useGraphExecution } from "./useGraphExecution";
import { getEvents, getInstance } from "../lib/graphsApi";
import type { GraphInstance, GraphNodeStatus } from "../lib/graphsApi";
import type { GraphTopologyEdge } from "./useGraphExecution.diff";

vi.mock("../lib/graphsApi", () => ({
  getInstance: vi.fn(),
  getEvents: vi.fn(),
}));
const mockGetInstance = vi.mocked(getInstance);
const mockGetEvents = vi.mocked(getEvents);

function node(name: string, status: string, id = `${name}-id`): GraphNodeStatus {
  return { node_name: name, node_id: id, status };
}

function instance(status: string, nodes: GraphNodeStatus[]): GraphInstance {
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

async function tick(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("useGraphExecution", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockGetInstance.mockReset();
    mockGetEvents.mockReset();
    mockGetEvents.mockResolvedValue([]);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads the instance immediately and polls every 2s while active", async () => {
    mockGetInstance.mockResolvedValue(instance("running", [node("a", "running")]));
    const { result } = renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));

    await tick(1);
    expect(result.current.instance?.status).toBe("running");
    expect(mockGetInstance).toHaveBeenCalledTimes(1);
    // Workspace + instance id are passed through (X-Workspace-Id header
    // resolution lives in graphsApi).
    expect(mockGetInstance).toHaveBeenCalledWith("ws1", "inst-1");

    await tick(2000);
    expect(mockGetInstance).toHaveBeenCalledTimes(2);
    await tick(4000);
    expect(mockGetInstance).toHaveBeenCalledTimes(4);
  });

  it("keeps polling a crashed instance (fault recovery may resume it)", async () => {
    mockGetInstance.mockResolvedValue(instance("crashed", [node("a", "crashed")]));
    renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));
    await tick(1);
    await tick(4000);
    expect(mockGetInstance.mock.calls.length).toBeGreaterThan(1);
  });

  it("stops polling once the instance reaches a terminal status", async () => {
    mockGetInstance
      .mockResolvedValueOnce(instance("running", [node("a", "running")]))
      .mockResolvedValue(instance("completed", [node("a", "completed")]));
    const { result } = renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));

    await tick(1);
    expect(mockGetInstance).toHaveBeenCalledTimes(1);
    await tick(2000);
    expect(mockGetInstance).toHaveBeenCalledTimes(2);
    expect(result.current.instance?.status).toBe("completed");

    await tick(10000);
    expect(mockGetInstance).toHaveBeenCalledTimes(2);
  });

  it("does not poll at all when the first snapshot is already terminal", async () => {
    mockGetInstance.mockResolvedValue(instance("stopped", []));
    renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));
    await tick(1);
    await tick(10000);
    expect(mockGetInstance).toHaveBeenCalledTimes(1);
  });

  it("cleans up the timer on unmount", async () => {
    mockGetInstance.mockResolvedValue(instance("running", [node("a", "running")]));
    const { unmount } = renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));
    await tick(1);
    await tick(2000);
    const calls = mockGetInstance.mock.calls.length;

    unmount();
    await tick(10000);
    expect(mockGetInstance.mock.calls.length).toBe(calls);
  });

  it("resets all state and rebaselines the diff when instanceId changes", async () => {
    mockGetInstance.mockImplementation((_ws, id) =>
      Promise.resolve(
        id === "inst-1"
          ? instance("running", [node("a", "completed")])
          : instance("running", [node("x", "running")], ),
      ),
    );
    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => useGraphExecution("ws1", id, EDGES),
      { initialProps: { id: "inst-1" } },
    );
    await tick(1);
    expect(result.current.instance?.graph_instance_id).toBe("inst-1");

    mockGetInstance.mockClear();
    rerender({ id: "inst-2" });
    // State is reset synchronously on the id change.
    expect(result.current.instance).toBeNull();
    expect(result.current.timeline).toEqual([]);
    expect(result.current.pulses).toEqual([]);

    await tick(1);
    expect(result.current.instance?.graph_instance_id).toBe("inst-1");
    // The new instance's first snapshot is a baseline: the already-completed
    // node must not fire a pulse.
    expect(result.current.pulses).toEqual([]);
    expect(result.current.timeline).toEqual([]);

    await tick(2000);
    expect(mockGetInstance).toHaveBeenCalledWith("ws1", "inst-2");
  });

  it("fires a deliver pulse on out-edges when a node completes", async () => {
    mockGetInstance
      .mockResolvedValueOnce(instance("running", [node("a", "running"), node("b", "pending")]))
      .mockResolvedValue(instance("running", [node("a", "completed"), node("b", "running")]));
    const { result } = renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));

    await tick(1);
    expect(result.current.pulses).toEqual([]);

    await tick(2000);
    expect(result.current.pulses).toHaveLength(1);
    expect(result.current.pulses[0]?.edge).toEqual({ source: "a", target: "b" });
    expect(result.current.pulses[0]?.id).toBe(1);
    // Node b going pending→running in the same frame fires no pulse.
    // The derived timeline gained node_completed + node_started.
    expect(result.current.timeline.map((event) => event.kind)).toEqual([
      "node_completed",
      "node_started",
    ]);
    expect(result.current.timeline.every((event) => event.derived)).toBe(true);

    // Signals stay monotonic: a later completion gets the next id.
    mockGetInstance.mockResolvedValue(
      instance("running", [node("a", "completed"), node("b", "completed")]),
    );
    await tick(2000);
    expect(result.current.pulses).toHaveLength(2);
    expect(result.current.pulses[1]?.edge).toEqual({ source: "b", target: "c" });
    expect(result.current.pulses[1]?.id).toBe(2);

    act(() => result.current.dismissPulse(1));
    expect(result.current.pulses.map((pulse) => pulse.id)).toEqual([2]);
  });

  it("emits a crash-flash signal (and no pulse) when a node crashes", async () => {
    mockGetInstance
      .mockResolvedValueOnce(instance("running", [node("a", "running")]))
      .mockResolvedValue(instance("crashed", [node("a", "crashed")]));
    const { result } = renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));

    await tick(1);
    await tick(2000);
    expect(result.current.pulses).toEqual([]);
    expect(result.current.crashFlashes).toHaveLength(1);
    expect(result.current.crashFlashes[0]).toMatchObject({ nodeId: "a-id", nodeName: "a" });
    expect(result.current.timeline.map((event) => event.kind)).toEqual(["node_crashed"]);

    act(() => result.current.dismissCrashFlash(result.current.crashFlashes[0]?.id ?? 0));
    expect(result.current.crashFlashes).toEqual([]);
  });

  it("merges REST events into the timeline and dedupes them across polls", async () => {
    mockGetInstance.mockResolvedValue(instance("running", [node("a", "running")]));
    mockGetEvents.mockResolvedValue([{ kind: "graph_completed" }]);
    const { result } = renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));

    await tick(1);
    expect(result.current.timeline.map((event) => event.kind)).toEqual(["graph_completed"]);
    expect(result.current.timeline[0]?.derived).toBe(false);

    // Same append-only list on the next poll → still one entry.
    await tick(2000);
    expect(result.current.timeline).toHaveLength(1);
    expect(mockGetEvents).toHaveBeenCalledWith("ws1", "inst-1");
  });

  it("surfaces getInstance failures as error and keeps polling", async () => {
    mockGetInstance
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(instance("running", [node("a", "running")]));
    const { result } = renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));

    await tick(1);
    expect(result.current.error).toBe("boom");
    expect(result.current.instance).toBeNull();

    await tick(2000);
    expect(result.current.error).toBeNull();
    expect(result.current.instance?.status).toBe("running");
  });

  it("refresh() re-fetches immediately without resetting state", async () => {
    mockGetInstance.mockResolvedValue(instance("running", [node("a", "running")]));
    const { result } = renderHook(() => useGraphExecution("ws1", "inst-1", EDGES));
    await tick(1);
    expect(mockGetInstance).toHaveBeenCalledTimes(1);

    act(() => result.current.refresh());
    await tick(0);
    expect(mockGetInstance).toHaveBeenCalledTimes(2);
    expect(result.current.instance?.status).toBe("running");
  });
});
