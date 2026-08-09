// Pure status-diff / timeline logic for `useGraphExecution` — no React
// imports, independently unit-testable. Implements the PRD §9.3 (Rev 2)
// transition table: deliver pulses fire ONLY on `*→completed` (the node's
// out-edges), never on `pending→running`, so one poll that observes both
// halves of a deliver does not double-fire the same edge.
//
// G11 additions: WS-mode helpers (patchNodeStatus, wsTimelineEvent,
// collectPrecisePulseEdge) — pure functions called by the hook with fields
// extracted from GraphWsMessage payloads.

import type { GraphEvent, GraphInstance, GraphNodeStatus } from "../lib/graphsApi";
import type { GraphOutputEvent } from "../lib/ws-client";

/**
 * Minimal topology edge, keyed by node name (matches GraphSpec edges).
 * Supplied by the caller's topology resolver (G01 parse/layout).
 */
export interface GraphTopologyEdge {
  source: string;
  target: string;
}

/** One observed node status change between two polls. */
export interface NodeStatusTransition {
  nodeId: string;
  nodeName: string;
  from: string;
  to: string;
  /** Client-side observation time (epoch ms). */
  timestamp: number;
}

export type DerivedEventKind =
  | "node_started"
  | "node_completed"
  | "node_crashed";

/** Unified timeline entry: diff-derived (client) or REST /events (server). */
export interface GraphTimelineEvent {
  key: string;
  kind: string;
  /** epoch ms — derived: transition time; REST: first-observed time. */
  timestamp: number;
  /** true = inferred from a status diff (Phase 1); false = REST /events. */
  derived: boolean;
  nodeId?: string;
  nodeName?: string;
  /** Original REST payload (absent for derived entries). */
  event?: GraphEvent;
}

/** Target statuses that derive a timeline event (§9.3). */
const DERIVED_KIND_BY_STATUS: Record<string, DerivedEventKind> = {
  running: "node_started",
  completed: "node_completed",
  crashed: "node_crashed",
};

/**
 * Compare two node-status snapshots. Nodes absent from `prev` establish the
 * baseline and produce no transition (first poll of an instance must not
 * fire pulses for work that happened before the viewer opened).
 */
export function diffNodeStatuses(
  prev: GraphNodeStatus[],
  current: GraphNodeStatus[],
  now: number,
): NodeStatusTransition[] {
  const prevById = new Map(prev.map((node) => [node.node_id, node]));
  const transitions: NodeStatusTransition[] = [];
  for (const node of current) {
    const before = prevById.get(node.node_id);
    if (!before || before.status === node.status) continue;
    transitions.push({
      nodeId: node.node_id,
      nodeName: node.node_name,
      from: before.status,
      to: node.status,
      timestamp: now,
    });
  }
  return transitions;
}

/**
 * §9.3 Rev 2: `*→completed` (including the `pending→completed` jump) fires a
 * deliver pulse on every out-edge of the completed node. Multiple
 * transitions in the same frame never fire the same edge twice.
 */
export function collectPulseEdges(
  transitions: NodeStatusTransition[],
  edges: GraphTopologyEdge[],
): GraphTopologyEdge[] {
  const seen = new Set<string>();
  const fired: GraphTopologyEdge[] = [];
  for (const transition of transitions) {
    if (transition.to !== "completed") continue;
    for (const edge of edges) {
      if (edge.source !== transition.nodeName) continue;
      const key = `${edge.source}->${edge.target}`;
      if (seen.has(key)) continue;
      seen.add(key);
      fired.push(edge);
    }
  }
  return fired;
}

/** `*→crashed` transitions — the node crash-flash signal (§9.3). */
export function collectCrashTransitions(
  transitions: NodeStatusTransition[],
): NodeStatusTransition[] {
  return transitions.filter((transition) => transition.to === "crashed");
}

/** Derive one local timeline event per mappable transition (§9.3). */
export function deriveTimelineEvents(
  transitions: NodeStatusTransition[],
): GraphTimelineEvent[] {
  const events: GraphTimelineEvent[] = [];
  for (const transition of transitions) {
    const kind = DERIVED_KIND_BY_STATUS[transition.to];
    if (!kind) continue;
    events.push({
      key: `derived:${transition.nodeId}:${transition.to}:${transition.timestamp}`,
      kind,
      timestamp: transition.timestamp,
      derived: true,
      nodeId: transition.nodeId,
      nodeName: transition.nodeName,
    });
  }
  return events;
}

/**
 * Wrap REST /events payloads as timeline entries. GraphEvent carries no
 * id/timestamp — key by per-kind ordinal, stable across polls because the
 * backend event list is append-only (same keying the pre-refactor
 * GraphExecutionViewer used); `observedAt` stands in for the timestamp.
 */
export function restTimelineEvents(
  events: GraphEvent[],
  observedAt: number,
): GraphTimelineEvent[] {
  const counts = new Map<string, number>();
  return events.map((event) => {
    const ordinal = counts.get(event.kind) ?? 0;
    counts.set(event.kind, ordinal + 1);
    return {
      key: `rest:${event.kind}:${ordinal}`,
      kind: event.kind,
      timestamp: observedAt,
      derived: false,
      event,
    };
  });
}

/**
 * Merge incoming entries into the timeline: dedupe by key (first-seen wins,
 * keeping the original timestamp) and sort ascending by timestamp.
 */
export function mergeTimelineEvents(
  existing: GraphTimelineEvent[],
  incoming: GraphTimelineEvent[],
): GraphTimelineEvent[] {
  const byKey = new Map<string, GraphTimelineEvent>();
  for (const event of existing) byKey.set(event.key, event);
  for (const event of incoming) {
    if (!byKey.has(event.key)) byKey.set(event.key, event);
  }
  const merged = [...byKey.values()].sort((a, b) => a.timestamp - b.timestamp);

  // Secondary dedupe: same kind + nodeId + timestamp → keep the richer entry.
  // REST entries (no nodeId) are keyed by primary key to avoid collapsing
  // distinct same-kind events that share a single observedAt timestamp.
  const seen = new Map<string, GraphTimelineEvent>();
  for (const e of merged) {
    const secKey =
      e.nodeId !== undefined
        ? `${e.kind}:${e.nodeId}:${e.timestamp}`
        : e.key;
    const prev = seen.get(secKey);
    if (!prev || (e.event && !prev.event)) {
      seen.set(secKey, e);
    }
  }
  return [...seen.values()];
}

// ── G11: WS-mode helpers (PRD §6.1 Phase 2, §11.2) ──────────────────────────

/**
 * Patch a single node's status in the instance (WS event-driven update).
 * Matches by node_id first, then node_name as fallback. Returns the original
 * instance if no matching node is found (the event may arrive before the
 * initial getInstance fetch resolves).
 */
export function patchNodeStatus(
  instance: GraphInstance | null,
  nodeId: string | undefined,
  nodeName: string | undefined,
  status: string,
): GraphInstance | null {
  if (!instance) return instance;
  let changed = false;
  const nodes = instance.nodes.map((n) => {
    if (nodeId && n.node_id === nodeId) {
      changed = true;
      return { ...n, status };
    }
    if (nodeName && n.node_name === nodeName) {
      changed = true;
      return { ...n, status };
    }
    return n;
  });
  return changed ? { ...instance, nodes } : instance;
}

/**
 * Create a timeline entry from a WS graph event (PRD §6.1 Phase 2). Uses the
 * backend timestamp and marks ``derived: false`` — the real event replaces
 * the polling-mode inferred entry for the same occurrence.
 */
export function wsTimelineEvent(
  kind: string,
  nodeId: string | undefined,
  nodeName: string | undefined,
  timestamp: number,
  event?: GraphOutputEvent,
): GraphTimelineEvent {
  return {
    key: `ws:${kind}:${nodeId ?? ""}:${timestamp}`,
    kind,
    timestamp,
    derived: false,
    nodeId,
    nodeName,
    event,
  };
}

/**
 * Resolve the precise edge for a ``deliver_dispatched`` WS event: look up
 * ``target_node_id`` → target node name in the instance's nodes, then find
 * the edge matching ``{source: sourceName, target: targetName}``. Returns
 * null if the target node or edge cannot be resolved (the instance may not
 * have loaded yet).
 */
export function collectPrecisePulseEdge(
  sourceName: string | undefined,
  targetNodeId: string | undefined,
  nodes: GraphNodeStatus[],
  edges: GraphTopologyEdge[],
): GraphTopologyEdge | null {
  if (!sourceName || !targetNodeId) return null;
  const targetNode = nodes.find((n) => n.node_id === targetNodeId);
  if (!targetNode) return null;
  return (
    edges.find(
      (e) => e.source === sourceName && e.target === targetNode.node_name,
    ) ?? null
  );
}
