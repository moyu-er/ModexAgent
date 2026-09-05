// useGraphExecution — state for one graph instance, with two data sources:
//
//  * **Polling mode** (Phase 1, PRD §6.1): 2s `getInstance` + `getEvents`
//    with client-side `diffNodeStatuses` to infer transitions → pulses.
//  * **WS mode** (Phase 2, G11, PRD §11.2): `subscribe_graph` over the shared
//    WebSocket; `graph_event` messages drive node status, precise deliver
//    pulses (source→target), and a real-timestamp timeline. No polling while
//    WS is connected; automatic fallback to 2s polling on disconnect, with
//    re-subscribe on reconnect.
//
// All diff/derive/merge/patch logic lives in the pure
// `./useGraphExecution.diff` module (PRD §9.3 + G11 helpers).

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getEvents,
  getInstance,
  type GraphInstance,
  type GraphNodeStatus,
  type GraphPayload,
} from "../lib/graphsApi";
import type {
  GraphOutputEvent,
  GraphWsMessage,
  WebSocketClient,
} from "../lib/ws-client";
import {
  collectCrashTransitions,
  collectPrecisePulseEdge,
  collectPulseEdges,
  deriveTimelineEvents,
  diffNodeStatuses,
  mergeTimelineEvents,
  patchNodeStatus,
  restTimelineEvents,
  wsTimelineEvent,
  type GraphTimelineEvent,
  type GraphTopologyEdge,
  type NodeStatusTransition,
} from "./useGraphExecution.diff";

// crashed stays polled — fault recovery may auto-resume it to running.
const ACTIVE_STATUSES = new Set(["pending", "running", "pausing", "paused", "stopping", "crashed"]);
const POLL_MS = 2000;
/** Pulse dedup window: if the same edge was pulsed within this window
 * (by either node_completed or deliver_dispatched), skip the duplicate.
 * The two events arrive within a few ms of each other; 500ms is well
 * within the 600ms pulse animation but short enough not to suppress
 * legitimate re-fires in a loop (which take seconds). */
const PULSE_DEDUP_MS = 500;

/** Deliver-pulse request for one edge; `id` increases monotonically per hook. */
export interface PulseSignal {
  id: number;
  edge: GraphTopologyEdge;
  timestamp: number;
}

/** Crash-flash request for one node; `id` increases monotonically per hook. */
export interface CrashSignal {
  id: number;
  nodeId: string;
  nodeName: string;
  timestamp: number;
}

export interface UseGraphExecutionResult {
  instance: GraphInstance | null;
  /** Derived + REST + WS events, deduped and sorted ascending by timestamp. */
  timeline: GraphTimelineEvent[];
  /** Unconsumed deliver-pulse signals (dismiss via `dismissPulse`). */
  pulses: PulseSignal[];
  /** Unconsumed crash-flash signals (dismiss via `dismissCrashFlash`). */
  crashFlashes: CrashSignal[];
  error: string | null;
  /** Re-fetch immediately (e.g. after a pause/resume/stop control). */
  refresh: () => Promise<void>;
  dismissPulse: (id: number) => void;
  dismissCrashFlash: (id: number) => void;
}

/**
 * Manages execution state for one graph instance.
 *
 * **Polling mode** (no `wsClient`): 2s poll `getInstance` + `getEvents`,
 * diff-based transition detection (PRD §9.3).
 *
 * **WS mode** (`wsClient` provided): subscribes to `graph_event` messages;
 * event-driven node status / precise deliver pulses / real-timestamp
 * timeline. Falls back to polling when the WS disconnects, re-subscribes
 * on reconnect. The initial `getInstance` fetch runs in both modes to
 * establish a baseline (the instance may have been running before the
 * viewer opened).
 *
 * `edges` is the spec topology ({source, target} by node name) used to
 * map `*→completed` transitions and `deliver_dispatched` events onto
 * deliver-pulse signals.
 */
export function useGraphExecution(
  workspaceId: string,
  instanceId: string,
  edges: GraphTopologyEdge[],
  wsClient?: WebSocketClient,
): UseGraphExecutionResult {
  const [instance, setInstance] = useState<GraphInstance | null>(null);
  const [timeline, setTimeline] = useState<GraphTimelineEvent[]>([]);
  const [pulses, setPulses] = useState<PulseSignal[]>([]);
  const [crashFlashes, setCrashFlashes] = useState<CrashSignal[]>([]);
  const [error, setError] = useState<string | null>(null);
  /** WS connection state — drives the polling fallback decision. */
  const [wsConnected, setWsConnected] = useState(false);

  const nextSignalId = useRef(1);
  const prevNodes = useRef<GraphNodeStatus[]>([]);
  // Bumped per effect run so a stale in-flight poll from a previous
  // instance/workspace can no longer write state.
  const generation = useRef(0);
  const snapshotRequest = useRef(0);
  const appliedSnapshot = useRef(0);
  // Only events received after a snapshot request began override that response.
  const liveStatus = useRef<Partial<Pick<GraphInstance, "status" | "result">> | null>(null);
  const edgesRef = useRef(edges);
  useEffect(() => {
    edgesRef.current = edges;
  }, [edges]);

  /** Mirrors instance.nodes for the WS event handler (node_id → node_name
   *  lookup for deliver_dispatched precise pulses). Also keeps prevNodes
   *  in sync so a manual refresh() or polling-fallback pollOnce() doesn't
   *  re-fire transitions that WS events already processed. */
  const instanceNodesRef = useRef<GraphNodeStatus[]>([]);
  useEffect(() => {
    instanceNodesRef.current = instance?.nodes ?? [];
    prevNodes.current = instance?.nodes ?? [];
  }, [instance?.nodes]);

  /** Recent pulse records for dedup: edgeKey → timestamp. Prevents
   *  deliver_dispatched from duplicating a node_completed pulse (and
   *  vice-versa) on the same edge within PULSE_DEDUP_MS. */
  const recentPulseEdges = useRef<Map<string, number>>(new Map());

  const useWs = !!wsClient;
  /** Poll when no WS client OR when the WS is disconnected (fallback). */
  const shouldPoll = !useWs || !wsConnected;

  // ── Polling (PRD §6.1 Phase 1) ────────────────────────────────────────────

  const pollOnce = useCallback(async (): Promise<void> => {
    const gen = generation.current;
    const request = ++snapshotRequest.current;
    const statusAtRequest = liveStatus.current;
    try {
      const snapshot = await getInstance(workspaceId, instanceId);
      if (gen !== generation.current || request < appliedSnapshot.current) return;
      appliedSnapshot.current = request;
      const loaded = liveStatus.current !== statusAtRequest
        ? { ...snapshot, ...liveStatus.current }
        : snapshot;
      const now = Date.now();
      const transitions = diffNodeStatuses(prevNodes.current, loaded.nodes, now);
      prevNodes.current = loaded.nodes;
      if (transitions.length > 0) {
        setTimeline((current) =>
          mergeTimelineEvents(current, deriveTimelineEvents(transitions)),
        );
        const firedEdges = collectPulseEdges(transitions, edgesRef.current);
        if (firedEdges.length > 0) {
          const signals: PulseSignal[] = firedEdges.map((edge) => ({
            id: nextSignalId.current++,
            edge,
            timestamp: now,
          }));
          setPulses((current) => [...current, ...signals]);
        }
        const crashes = collectCrashTransitions(transitions);
        if (crashes.length > 0) {
          const signals: CrashSignal[] = crashes.map((transition) => ({
            id: nextSignalId.current++,
            nodeId: transition.nodeId,
            nodeName: transition.nodeName,
            timestamp: now,
          }));
          setCrashFlashes((current) => [...current, ...signals]);
        }
      }
      setInstance(loaded);
      setError(null);
    } catch (err) {
      if (gen !== generation.current || request < appliedSnapshot.current) return;
      setError(err instanceof Error ? err.message : String(err));
    }
    getEvents(workspaceId, instanceId)
      .then((loaded) => {
        if (gen !== generation.current) return;
        setTimeline((current) =>
          mergeTimelineEvents(current, restTimelineEvents(loaded, Date.now())),
        );
      })
      .catch(() => {
        // Event polling is best-effort; the instance payload carries status.
      });
  }, [workspaceId, instanceId]);

  // ── Reset on instance/workspace switch ───────────────────────────────────

  useEffect(() => {
    generation.current += 1;
    liveStatus.current = null;
    prevNodes.current = [];
    recentPulseEdges.current.clear();
    setInstance(null);
    setTimeline([]);
    setPulses([]);
    setCrashFlashes([]);
    setError(null);
    setWsConnected(false);
    return () => { generation.current += 1; };
  }, [instanceId, workspaceId]);

  // ── Polling effect (polling mode + WS-disconnect fallback) ───────────────

  useEffect(() => {
    if (shouldPoll) void pollOnce();
  }, [pollOnce, shouldPoll]);

  const active = instance === null || ACTIVE_STATUSES.has(instance.status);
  useEffect(() => {
    if (!shouldPoll || !active) return;
    const timer = setInterval(() => { void pollOnce(); }, POLL_MS);
    return () => clearInterval(timer);
  }, [pollOnce, shouldPoll, active]);

  // ── WS mode (G11, PRD §11.2 Phase 2) ─────────────────────────────────────

  /** Check whether an edge was pulsed within the dedup window; if not,
   *  record it and return true (fire). If yes, return false (skip). */
  const shouldFirePulse = useCallback(
    (edgeKey: string, ts: number): boolean => {
      const last = recentPulseEdges.current.get(edgeKey);
      if (last !== undefined && ts - last < PULSE_DEDUP_MS) {
        return false;
      }
      recentPulseEdges.current.set(edgeKey, ts);
      return true;
    },
    [],
  );

  /** Dispatch a single GraphOutputEvent to state + signal updates. Pure
   *  side-effect function — reads only refs and stable setters, so it's
   *  safe to define inside the useCallback below. */
  const handleGraphMessage = useCallback(
    (msg: GraphWsMessage): void => {
      if (msg.type === "graph_error") {
        setError(msg.message);
        return;
      }
      if (msg.type === "graph_subscribed" && msg.graph_instance_id === instanceId) {
        // Subscribe first, then reconcile anything missed before the ack.
        void pollOnce();
      }
      if (msg.type !== "graph_event") return;
      if (msg.graph_instance_id !== instanceId) return;

      const event: GraphOutputEvent = msg.event;
      const ts = event.timestamp ?? Date.now();

      switch (event.kind) {
        case "node_started": {
          setInstance((prev) =>
            patchNodeStatus(prev, event.node_id, event.node_name, "running"),
          );
          setTimeline((current) =>
            mergeTimelineEvents(current, [
              wsTimelineEvent("node_started", event.node_id, event.node_name, ts, event),
            ]),
          );
          break;
        }
        case "node_completed": {
          setInstance((prev) =>
            patchNodeStatus(prev, event.node_id, event.node_name, "completed"),
          );
          // Fire out-edge pulses (deduped against recent deliver_dispatched).
          const nodeName = event.node_name;
          if (nodeName) {
            const transition: NodeStatusTransition = {
              nodeId: event.node_id ?? "",
              nodeName,
              from: "running",
              to: "completed",
              timestamp: ts,
            };
            const firedEdges = collectPulseEdges([transition], edgesRef.current);
            const newSignals: PulseSignal[] = [];
            for (const edge of firedEdges) {
              const key = `${edge.source}->${edge.target}`;
              if (shouldFirePulse(key, ts)) {
                newSignals.push({
                  id: nextSignalId.current++,
                  edge,
                  timestamp: ts,
                });
              }
            }
            if (newSignals.length > 0) {
              setPulses((current) => [...current, ...newSignals]);
            }
          }
          setTimeline((current) =>
            mergeTimelineEvents(current, [
              wsTimelineEvent("node_completed", event.node_id, event.node_name, ts, event),
            ]),
          );
          break;
        }
        case "node_crashed": {
          setInstance((prev) =>
            patchNodeStatus(prev, event.node_id, event.node_name, "crashed"),
          );
          if (event.node_id && event.node_name) {
            setCrashFlashes((current) => [
              ...current,
              {
                id: nextSignalId.current++,
                nodeId: event.node_id!,
                nodeName: event.node_name!,
                timestamp: ts,
              },
            ]);
          }
          setTimeline((current) =>
            mergeTimelineEvents(current, [
              wsTimelineEvent("node_crashed", event.node_id, event.node_name, ts, event),
            ]),
          );
          break;
        }
        case "deliver_dispatched": {
          // Precise pulse: source_node_id → target_node_id (resolved to edge).
          const edge = collectPrecisePulseEdge(
            event.node_name,
            event.target_node_id,
            instanceNodesRef.current,
            edgesRef.current,
          );
          if (edge) {
            const key = `${edge.source}->${edge.target}`;
            if (shouldFirePulse(key, ts)) {
              setPulses((current) => [
                ...current,
                {
                  id: nextSignalId.current++,
                  edge,
                  timestamp: ts,
                },
              ]);
            }
          }
          setTimeline((current) =>
            mergeTimelineEvents(current, [
              wsTimelineEvent("deliver_dispatched", event.node_name, event.node_name, ts, event),
            ]),
          );
          break;
        }
        case "graph_status_changed":
        case "graph_completed":
        case "graph_failed":
        case "graph_crashed": {
          const status = event.kind === "graph_status_changed" ? event.status
            : event.kind === "graph_completed" ? "completed"
            : event.kind === "graph_failed" ? "failed" : "crashed";
          if (status) {
            const patch = {
              status,
              ...(event.kind === "graph_completed"
                ? { result: (event.result as GraphPayload[] | null) ?? null }
                : {}),
            };
            liveStatus.current = patch;
            setInstance((prev) => prev ? { ...prev, ...patch } : prev);
          }
          setTimeline((current) =>
            mergeTimelineEvents(current, [
              wsTimelineEvent(event.kind, undefined, undefined, ts, event),
            ]),
          );
          break;
        }
      }
    },
    [instanceId, shouldFirePulse, pollOnce],
  );

  // ── WS subscription lifecycle ────────────────────────────────────────────

  useEffect(() => {
    if (!wsClient) return;

    // Register the graph message handler.
    wsClient.setGraphHandler(handleGraphMessage);

    // Connection listener: drives wsConnected (→ polling fallback) and
    // re-subscribes on reconnect.
    const onConnChange = (connected: boolean): void => {
      setWsConnected(connected);
      if (connected) {
        wsClient.send("subscribe_graph", {
          instance_id: instanceId,
          ...(workspaceId ? { ws: workspaceId } : {}),
        });
      }
    };
    wsClient.addConnectionListener(onConnChange);

    // If already connected, subscribe immediately.
    if (wsClient.connected) {
      setWsConnected(true);
      wsClient.send("subscribe_graph", {
        instance_id: instanceId,
        ...(workspaceId ? { ws: workspaceId } : {}),
      });
    }

    return (): void => {
      wsClient.setGraphHandler(null);
      wsClient.removeConnectionListener(onConnChange);
      if (wsClient.connected) {
        wsClient.send("unsubscribe_graph", {
          instance_id: instanceId,
          ...(workspaceId ? { ws: workspaceId } : {}),
        });
      }
    };
  }, [wsClient, instanceId, workspaceId, handleGraphMessage]);

  const refresh = useCallback(async (): Promise<void> => {
    await pollOnce();
  }, [pollOnce]);

  const dismissPulse = useCallback((id: number): void => {
    setPulses((current) => current.filter((pulse) => pulse.id !== id));
  }, []);

  const dismissCrashFlash = useCallback((id: number): void => {
    setCrashFlashes((current) => current.filter((flash) => flash.id !== id));
  }, []);

  return {
    instance,
    timeline,
    pulses,
    crashFlashes,
    error,
    refresh,
    dismissPulse,
    dismissCrashFlash,
  };
}
