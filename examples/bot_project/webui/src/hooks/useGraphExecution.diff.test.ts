import { describe, expect, it } from "vitest";
import type { GraphEvent, GraphNodeStatus } from "../lib/graphsApi";
import {
  collectCrashTransitions,
  collectPulseEdges,
  deriveTimelineEvents,
  diffNodeStatuses,
  mergeTimelineEvents,
  restTimelineEvents,
  type GraphTimelineEvent,
  type GraphTopologyEdge,
} from "./useGraphExecution.diff";

const NOW = 1_700_000_000_000;

function node(name: string, status: string, id = `${name}-id`): GraphNodeStatus {
  return { node_name: name, node_id: id, status };
}

describe("diffNodeStatuses", () => {
  it("records a pending→running transition (status recolor only)", () => {
    const transitions = diffNodeStatuses(
      [node("a", "pending")],
      [node("a", "running")],
      NOW,
    );
    expect(transitions).toEqual([
      { nodeId: "a-id", nodeName: "a", from: "pending", to: "running", timestamp: NOW },
    ]);
  });

  it("records a running→completed transition", () => {
    const transitions = diffNodeStatuses(
      [node("a", "running")],
      [node("a", "completed")],
      NOW,
    );
    expect(transitions).toHaveLength(1);
    expect(transitions[0]?.to).toBe("completed");
  });

  it("records the pending→completed jump (whole lifecycle inside one poll)", () => {
    const transitions = diffNodeStatuses(
      [node("a", "pending")],
      [node("a", "completed")],
      NOW,
    );
    expect(transitions).toHaveLength(1);
    expect(transitions[0]?.from).toBe("pending");
    expect(transitions[0]?.to).toBe("completed");
  });

  it("records a *→crashed transition", () => {
    const transitions = diffNodeStatuses(
      [node("a", "running")],
      [node("a", "crashed")],
      NOW,
    );
    expect(transitions[0]?.to).toBe("crashed");
  });

  it("records other transitions (e.g. running→paused)", () => {
    const transitions = diffNodeStatuses(
      [node("a", "running")],
      [node("a", "paused")],
      NOW,
    );
    expect(transitions[0]?.to).toBe("paused");
  });

  it("treats the first snapshot as baseline — no transitions for unseen nodes", () => {
    const transitions = diffNodeStatuses(
      [],
      [node("a", "completed"), node("b", "running")],
      NOW,
    );
    expect(transitions).toEqual([]);
  });

  it("ignores unchanged statuses", () => {
    const transitions = diffNodeStatuses(
      [node("a", "running"), node("b", "pending")],
      [node("a", "running"), node("b", "completed")],
      NOW,
    );
    expect(transitions).toHaveLength(1);
    expect(transitions[0]?.nodeName).toBe("b");
  });
});

describe("collectPulseEdges (§9.3 transition table)", () => {
  const edges: GraphTopologyEdge[] = [
    { source: "a", target: "b" },
    { source: "a", target: "c" },
    { source: "b", target: "c" },
  ];

  it("pending→running fires no pulse", () => {
    const transitions = diffNodeStatuses(
      [node("a", "pending")],
      [node("a", "running")],
      NOW,
    );
    expect(collectPulseEdges(transitions, edges)).toEqual([]);
  });

  it("*→completed fires pulses on all out-edges of the node", () => {
    const transitions = diffNodeStatuses(
      [node("a", "running")],
      [node("a", "completed")],
      NOW,
    );
    expect(collectPulseEdges(transitions, edges)).toEqual([
      { source: "a", target: "b" },
      { source: "a", target: "c" },
    ]);
  });

  it("pending→completed jump fires the same out-edge pulses", () => {
    const transitions = diffNodeStatuses(
      [node("a", "pending")],
      [node("a", "completed")],
      NOW,
    );
    expect(collectPulseEdges(transitions, edges)).toHaveLength(2);
  });

  it("*→crashed fires no pulse", () => {
    const transitions = diffNodeStatuses(
      [node("a", "running")],
      [node("a", "crashed")],
      NOW,
    );
    expect(collectPulseEdges(transitions, edges)).toEqual([]);
  });

  it("other transitions (running→paused) fire no pulse", () => {
    const transitions = diffNodeStatuses(
      [node("a", "running")],
      [node("a", "paused")],
      NOW,
    );
    expect(collectPulseEdges(transitions, edges)).toEqual([]);
  });

  it("dedupes: multiple transitions in one frame never fire the same edge twice", () => {
    const duplicated: GraphTopologyEdge[] = [
      { source: "a", target: "b" },
      { source: "a", target: "b" },
      { source: "b", target: "a" },
    ];
    const transitions = diffNodeStatuses(
      [node("a", "running"), node("b", "running")],
      [node("a", "completed"), node("b", "completed")],
      NOW,
    );
    expect(collectPulseEdges(transitions, duplicated)).toEqual([
      { source: "a", target: "b" },
      { source: "b", target: "a" },
    ]);
  });
});

describe("collectCrashTransitions", () => {
  it("keeps only *→crashed transitions", () => {
    const transitions = diffNodeStatuses(
      [node("a", "running"), node("b", "running")],
      [node("a", "crashed"), node("b", "completed")],
      NOW,
    );
    const crashes = collectCrashTransitions(transitions);
    expect(crashes).toHaveLength(1);
    expect(crashes[0]?.nodeName).toBe("a");
  });
});

describe("deriveTimelineEvents", () => {
  it("maps transitions to node_started / node_completed / node_crashed, marked derived", () => {
    const transitions = [
      { nodeId: "a-id", nodeName: "a", from: "pending", to: "running", timestamp: NOW },
      { nodeId: "b-id", nodeName: "b", from: "running", to: "completed", timestamp: NOW + 1 },
      { nodeId: "c-id", nodeName: "c", from: "running", to: "crashed", timestamp: NOW + 2 },
      { nodeId: "d-id", nodeName: "d", from: "running", to: "paused", timestamp: NOW + 3 },
    ];
    const events = deriveTimelineEvents(transitions);
    expect(events.map((event) => event.kind)).toEqual([
      "node_started",
      "node_completed",
      "node_crashed",
    ]);
    for (const event of events) {
      expect(event.derived).toBe(true);
      expect(event.event).toBeUndefined();
    }
    expect(events[0]).toMatchObject({ nodeId: "a-id", nodeName: "a", timestamp: NOW });
  });
});

describe("restTimelineEvents", () => {
  it("keys events by per-kind ordinal, stable across polls", () => {
    const rest: GraphEvent[] = [
      { kind: "graph_completed" },
      { kind: "graph_completed" },
      { kind: "graph_crashed", error: "boom" },
    ];
    const events = restTimelineEvents(rest, NOW);
    expect(events.map((event) => event.key)).toEqual([
      "rest:graph_completed:0",
      "rest:graph_completed:1",
      "rest:graph_crashed:0",
    ]);
    for (const event of events) {
      expect(event.derived).toBe(false);
      expect(event.timestamp).toBe(NOW);
    }
    expect(events[2]?.event?.error).toBe("boom");
  });
});

describe("mergeTimelineEvents", () => {
  const entry = (key: string, timestamp: number): GraphTimelineEvent => ({
    key,
    kind: key,
    timestamp,
    derived: key.startsWith("derived"),
  });

  it("dedupes by key, keeping the first-seen entry (and its timestamp)", () => {
    const existing = [entry("rest:graph_completed:0", NOW)];
    const incoming = [entry("rest:graph_completed:0", NOW + 2000)];
    const merged = mergeTimelineEvents(existing, incoming);
    expect(merged).toHaveLength(1);
    expect(merged[0]?.timestamp).toBe(NOW);
  });

  it("merges derived and REST events into one ascending-by-time timeline", () => {
    const existing = [entry("rest:graph_completed:0", NOW + 10)];
    const incoming = [
      entry("derived:a:running", NOW),
      entry("derived:a:completed", NOW + 5),
    ];
    const merged = mergeTimelineEvents(existing, incoming);
    expect(merged.map((event) => event.key)).toEqual([
      "derived:a:running",
      "derived:a:completed",
      "rest:graph_completed:0",
    ]);
  });

  it("is a no-op when incoming entries are all duplicates", () => {
    const existing = [entry("a", NOW), entry("b", NOW + 1)];
    expect(mergeTimelineEvents(existing, existing)).toEqual(existing);
  });
});
