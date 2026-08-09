// GraphExecutionViewer — hero view of the graph visualization redesign
// (PRD §6.1, ticket G05). Replaces the flat node-list + event-list with a
// full-canvas topology + context sidebar layout.
//
// Layout (desktop ≥768px):
//   ┌─ Top control bar (Back · instance id · status · Pause/Resume/Stop/Deliver)
//   ├─┬─ Topology canvas (flex-1, SVG) ──────────┬─ Sidebar (w=320, fixed)
//   │ └─ Bottom summary bar (progress/elapsed/…)  │  Node detail / Instance summary
//   │                                              │  Event timeline (bottom)
//   └──────────────────────────────────────────────┴──────────
//
// Data flow (Phase 1, PRD §6.1):
//   instance.spec_id → getSpec → parseGraphSpecYaml → topology → layoutGraph
//   useGraphExecution(ws, id, edges, wsClient) → instance/timeline/pulses → canvas+sidebar
//   When wsClient is provided (G11, Phase 2): WS subscribe_graph drives
//   event-based updates; polling is the disconnect fallback.
//
// Small screen (≤768px): canvas replaced by MiniTopology + simplified node
// list; sidebar stacks below.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FC,
} from "react";
import {
  ArrowLeft,
  ExternalLink,
  Pause,
  Play,
  Send,
  Square,
} from "lucide-react";
import {
  deliverToNode,
  getSpec,
  pauseGraph,
  resumeGraph,
  stopGraph,
} from "../../lib/graphsApi";
import { useGraphExecution } from "../../hooks/useGraphExecution";
import type { WebSocketClient } from "../../lib/ws-client";
import { useT, type MessageKey } from "../../i18n";
import { useToast } from "../ToastContext";
import { Button } from "../ui/Button";
import { SectionLabel } from "../ui/SectionLabel";
import {
  TopologyCanvas,
  type PulseSignal as CanvasPulseSignal,
} from "./topology/TopologyCanvas";
import { MiniTopology } from "./topology/MiniTopology";
import type { GraphNodeVisualStatus } from "./topology/GraphNode";
import { edgeKey, layoutGraph } from "./topology/layout";
import {
  GRAPH_NODE_END,
  GRAPH_NODE_START,
  parseGraphSpecYaml,
  type ParsedGraphTopology,
} from "./yaml/parseGraphSpec";
import { formatGraphApiError, GraphStatusBadge } from "./shared";
import { NodeDetailPanel } from "./detail/NodeDetailPanel";
import { DropdownPanel } from "../ui/DropdownPanel";
import { Textarea } from "../ui/Textarea";
import { InstanceSummary } from "./detail/InstanceSummary";
import { EventTimeline } from "./detail/EventTimeline";

// ── Constants ────────────────────────────────────────────────────────────────

const ACTIVE_STATUSES = new Set(["pending", "running", "paused", "crashed"]);
/** Crash-flash auto-dismiss (PRD §8.1: --dur = 220ms). */
const CRASH_FLASH_MS = 220;

const STATUS_LABEL_KEYS: Record<string, MessageKey> = {
  pending: "graphs.statusPending",
  running: "graphs.statusRunning",
  paused: "graphs.statusPaused",
  stopped: "graphs.statusStopped",
  crashed: "graphs.statusCrashed",
  completed: "graphs.statusCompleted",
  failed: "graphs.statusFailed",
};

export function statusLabelKey(status: string): MessageKey {
  return STATUS_LABEL_KEYS[status] ?? "graphs.status";
}

const VALID_VISUAL_STATUSES = new Set([
  "pending",
  "running",
  "completed",
  "crashed",
  "canceled",
]);

function toVisualStatus(status: string): GraphNodeVisualStatus {
  return VALID_VISUAL_STATUSES.has(status)
    ? (status as GraphNodeVisualStatus)
    : "pending";
}

/** Functional node count (excludes __start__/__end__ virtual endpoints). */
function functionalNodeCount(topology: ParsedGraphTopology): number {
  return topology.nodes.filter(
    (n) => n.nodeType !== GRAPH_NODE_START && n.nodeType !== GRAPH_NODE_END,
  ).length;
}

// ── Component ────────────────────────────────────────────────────────────────

export interface GraphExecutionViewerProps {
  workspaceId: string;
  instanceId: string;
  /** Live WS client from useWebUIStream — when provided, useGraphExecution
   *  enters WS mode (subscribe_graph + event-driven pulses, no 2s polling).
   *  Undefined in tests or before the WS connects → polling fallback. */
  wsClient?: WebSocketClient;
  onBack: () => void;
  onJumpToSession: (sessionId: string) => void;
}

export const GraphExecutionViewer: FC<GraphExecutionViewerProps> = ({
  workspaceId,
  instanceId,
  wsClient,
  onBack,
  onJumpToSession,
}) => {
  const t = useT();
  const toast = useToast();

  // ── State ────────────────────────────────────────────────────────────────

  const [topology, setTopology] = useState<ParsedGraphTopology | null>(null);
  const [specInfo, setSpecInfo] = useState<{
    name: string;
    version: string;
  } | null>(null);
  const [topologyError, setTopologyError] = useState<string | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [deliverNodeName, setDeliverNodeName] = useState("");
  const [deliverContent, setDeliverContent] = useState("");
  const [controlBusy, setControlBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [runStartTime, setRunStartTime] = useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  // ── Derived: edges for the hook ───────────────────────────────────────────

  const edges = useMemo(
    () => topology?.edges ?? [],
    [topology],
  );

  // ── Core hook ─────────────────────────────────────────────────────────────

  const {
    instance,
    timeline,
    pulses,
    crashFlashes,
    error: pollError,
    refresh,
    dismissPulse,
    dismissCrashFlash,
  } = useGraphExecution(workspaceId, instanceId, edges, wsClient);

  // ── Derived: layout + canvas pulses + node statuses ──────────────────────

  const layout = useMemo(
    () => (topology ? layoutGraph(topology) : null),
    [topology],
  );

  const canvasPulses = useMemo<CanvasPulseSignal[]>(() => {
    if (!layout) return [];
    const result: CanvasPulseSignal[] = [];
    for (const pulse of pulses) {
      const key = edgeKey(pulse.edge.source, pulse.edge.target);
      const laid = layout.edges.get(key);
      if (!laid) continue;
      result.push({ id: pulse.id, edgeKey: key, points: laid.points });
    }
    return result;
  }, [pulses, layout]);

  const activeEdges = useMemo(() => {
    const set = new Set<string>();
    for (const pulse of pulses) {
      set.add(edgeKey(pulse.edge.source, pulse.edge.target));
    }
    return set;
  }, [pulses]);

  const nodeStatuses = useMemo<Record<string, GraphNodeVisualStatus>>(() => {
    const map: Record<string, GraphNodeVisualStatus> = {};
    for (const node of instance?.nodes ?? []) {
      map[node.node_name] = toVisualStatus(node.status);
    }
    return map;
  }, [instance?.nodes]);

  const totalNodes = useMemo(
    () => (topology ? functionalNodeCount(topology) : 0),
    [topology],
  );

  const completedCount = useMemo(
    () =>
      instance?.nodes.filter(
        (n) =>
          n.status === "completed" &&
          n.node_name !== GRAPH_NODE_START &&
          n.node_name !== GRAPH_NODE_END,
      ).length ?? 0,
    [instance?.nodes],
  );

  // ── Effects ───────────────────────────────────────────────────────────────

  // Spec fetch: instance.spec_id → getSpec → parseGraphSpecYaml → topology.
  // Runs once when spec_id becomes available.
  useEffect(() => {
    const specId = instance?.spec_id;
    if (!specId) return;
    let cancelled = false;
    setTopologyError(null);
    getSpec(workspaceId, specId)
      .then((spec) => {
        if (cancelled) return;
        setSpecInfo({ name: spec.name, version: spec.version });
        try {
          setTopology(parseGraphSpecYaml(spec.yaml_content));
        } catch (err) {
          setTopologyError(
            err instanceof Error ? err.message : String(err),
          );
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setTopologyError(formatGraphApiError(err));
      });
    return () => {
      cancelled = true;
    };
  }, [instance?.spec_id, workspaceId]);

  // Reset state when instanceId changes.
  useEffect(() => {
    setTopology(null);
    setSpecInfo(null);
    setTopologyError(null);
    setSelectedNodeId(null);
    setDeliverNodeName("");
    setDeliverContent("");
    setRunStartTime(null);
    setElapsedSeconds(0);
    setActionError(null);
  }, [instanceId]);

  // Track when the instance first enters running state (for elapsed timer).
  useEffect(() => {
    if (instance?.status === "running" && runStartTime === null) {
      setRunStartTime(Date.now());
    }
  }, [instance?.status, runStartTime]);

  // Elapsed seconds ticker.
  useEffect(() => {
    if (runStartTime === null) return;
    const update = (): void => {
      setElapsedSeconds(Math.floor((Date.now() - runStartTime) / 1000));
    };
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [runStartTime]);

  // Auto-dismiss crash flashes after 220ms (PRD §8.1).
  // Each flash is scheduled exactly once.
  const flashTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(
    new Map(),
  );
  useEffect(() => {
    for (const flash of crashFlashes) {
      if (!flashTimers.current.has(flash.id)) {
        const timer = setTimeout(() => {
          dismissCrashFlash(flash.id);
          flashTimers.current.delete(flash.id);
        }, CRASH_FLASH_MS);
        flashTimers.current.set(flash.id, timer);
      }
    }
  }, [crashFlashes, dismissCrashFlash]);

  // ── Handlers ──────────────────────────────────────────────────────────────

  const handleOpenSession = useCallback(
    (nodeName: string): void => {
      const node = instance?.nodes.find((n) => n.node_name === nodeName);
      if (node?.session_id) {
        onJumpToSession(node.session_id);
      }
    },
    [instance?.nodes, onJumpToSession],
  );

  const handleControl = useCallback(
    (fn: typeof pauseGraph): void => {
      setControlBusy(true);
      setActionError(null);
      fn(workspaceId, instanceId)
        .then(() => refresh())
        .catch((err) => setActionError(formatGraphApiError(err)))
        .finally(() => setControlBusy(false));
    },
    [workspaceId, instanceId, refresh],
  );

  const handleDeliverInline = useCallback((): void => {
    if (!deliverNodeName || !deliverContent.trim() || controlBusy) return;
    setActionError(null);
    deliverToNode(workspaceId, instanceId, deliverNodeName, deliverContent)
      .then(() => {
        refresh();
        toast.show({
          message: t("graphs.deliverSuccess", { name: deliverNodeName }),
        });
        setDeliverContent("");
      })
      .catch((err) => setActionError(formatGraphApiError(err)));
  }, [workspaceId, instanceId, deliverNodeName, deliverContent, controlBusy, refresh, toast, t]);

  // ── Computed ──────────────────────────────────────────────────────────────

  const status = instance?.status ?? "";
  const canPause = status === "running" || status === "pending";
  const canResume = status === "paused";
  const canStop = ACTIVE_STATUSES.has(status);
  const canDeliver = status === "running" || status === "paused";
  const error = actionError ?? pollError;

  // Selected node lookups.
  const selectedParsedNode = topology?.nodes.find(
    (n) => n.name === selectedNodeId,
  );
  const selectedInstanceNode = instance?.nodes.find(
    (n) => n.node_name === selectedNodeId,
  );
  const selectedVisualStatus: GraphNodeVisualStatus = selectedNodeId
    ? (nodeStatuses[selectedNodeId] ?? "pending")
    : "pending";

  // Deliver node options (functional nodes from instance).
  const deliverNodeNames = useMemo(
    () =>
      instance?.nodes
        .filter(
          (n) =>
            n.node_name !== GRAPH_NODE_START &&
            n.node_name !== GRAPH_NODE_END,
        )
        .map((n) => n.node_name) ?? [],
    [instance?.nodes],
  );

  // Auto-select the first functional node when deliver becomes available.
  useEffect(() => {
    if (canDeliver && !deliverNodeName && deliverNodeNames.length > 0) {
      setDeliverNodeName(deliverNodeNames[0] ?? "");
    }
  }, [canDeliver, deliverNodeName, deliverNodeNames]);

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div
      className="flex flex-1 flex-col min-h-0"
      data-testid="graph-execution-viewer"
    >
      {/* A. Top control bar */}
      <div
        className="flex items-center gap-3 border-b border-hairline px-4 py-3"
        data-testid="control-bar"
      >
        <Button
          variant="ghost"
          size="sm"
          onClick={onBack}
          className="gap-1.5 -ml-2"
        >
          <ArrowLeft size={14} />
          {t("graphs.back")}
        </Button>
        <span className="font-mono text-base text-ink">{instanceId}</span>
        {instance ? (
          <GraphStatusBadge
            status={status}
            label={t(statusLabelKey(status))}
          />
        ) : null}
        <div className="ml-auto flex items-center gap-2">
          {canPause ? (
            <Button
              variant="secondary"
              size="sm"
              disabled={controlBusy}
              onClick={(): void => handleControl(pauseGraph)}
            >
              <Pause size={14} />
              {t("graphs.pause")}
            </Button>
          ) : null}
          {canResume ? (
            <Button
              variant="secondary"
              size="sm"
              disabled={controlBusy}
              onClick={(): void => handleControl(resumeGraph)}
            >
              <Play size={14} />
              {t("graphs.resume")}
            </Button>
          ) : null}
          {canStop ? (
            <Button
              variant="danger"
              size="sm"
              disabled={controlBusy}
              onClick={(): void => handleControl(stopGraph)}
            >
              <Square size={14} />
              {t("graphs.stop")}
            </Button>
          ) : null}
        </div>
      </div>

      {/* Error banner (polling or action errors) */}
      {error ? (
        <pre className="mx-4 mt-3 whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
          {error}
        </pre>
      ) : null}

      {/* Main area: canvas + sidebar (row on desktop, column on mobile) */}
      <div className="flex flex-col min-h-0 flex-1 md:flex-row">
        {/* Canvas area — hidden on small screens */}
        <div className="hidden md:flex md:flex-1 md:flex-col md:min-w-0">
          {topology ? (
            <TopologyCanvas
              topology={topology}
              nodeStatuses={nodeStatuses}
              activeEdges={activeEdges}
              pulses={canvasPulses}
              onPulseComplete={dismissPulse}
              selectedNodeId={selectedNodeId}
              onSelectNode={setSelectedNodeId}
              onOpenSession={handleOpenSession}
              className="flex-1"
            />
          ) : (
            <div className="flex flex-1 items-center justify-center">
              {topologyError ? (
                <pre className="max-w-md whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
                  {topologyError}
                </pre>
              ) : (
                <p className="text-base text-mute">{t("graphs.loading")}</p>
              )}
            </div>
          )}

          {/* C. Bottom summary bar */}
          <div
            className="flex items-center gap-4 border-t border-hairline px-4 py-2 font-mono text-xs text-mute"
            data-testid="summary-bar"
          >
            <span>
              {t("graphs.progress", {
                completed: completedCount,
                total: totalNodes,
              })}
            </span>
            <span>{t("graphs.elapsed", { seconds: elapsedSeconds })}</span>
            {topology ? (
              <>
                <span>{topology.scheduler}</span>
                <span>{topology.defaultTrigger}</span>
              </>
            ) : null}
          </div>
        </div>

        {/* Small screen layout — hidden on desktop */}
        <div className="flex-1 overflow-y-auto md:hidden">
          {topology ? (
            <div className="flex items-center gap-3 border-b border-hairline px-4 py-2">
              <MiniTopology topology={topology} nodeStatuses={nodeStatuses} />
              <div className="flex flex-col gap-0.5 font-mono text-xs text-mute">
                <span>
                  {t("graphs.progress", {
                    completed: completedCount,
                    total: totalNodes,
                  })}
                </span>
                <span>
                  {t("graphs.elapsed", { seconds: elapsedSeconds })}
                </span>
              </div>
            </div>
          ) : null}
          <SectionLabel>{t("graphs.nodes")}</SectionLabel>
          {!instance ? (
            <p className="px-4 text-base text-mute">{t("graphs.loading")}</p>
          ) : instance.nodes.length === 0 ? (
            <p className="px-4 text-base text-mute">{t("graphs.noNodes")}</p>
          ) : (
            <div className="flex flex-col">
              {instance.nodes.map((node) => (
                <button
                  key={node.node_id}
                  type="button"
                  onClick={(): void => {
                    if (node.session_id) {
                      onJumpToSession(node.session_id);
                    }
                  }}
                  title={t("graphs.openSession")}
                  className="flex items-center justify-between gap-2 px-4 py-2.5 text-left hover:bg-hairline-soft"
                >
                  <span className="flex min-w-0 items-center gap-2.5">
                    <span className="truncate text-base text-ink">
                      {node.node_name}
                    </span>
                    <span className="truncate font-mono text-xs text-faint">
                      {node.node_id}
                    </span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2">
                    <GraphStatusBadge
                      status={node.status}
                      label={t(statusLabelKey(node.status))}
                    />
                    <ExternalLink size={14} className="text-mute" />
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>

        {/* D. Context sidebar */}
        <aside className="flex w-full flex-col border-t border-hairline md:w-80 md:border-l md:border-t-0">
          {/* Top: node detail or instance summary */}
          <div className="flex-1 overflow-y-auto p-4">
            {selectedNodeId && selectedParsedNode ? (
              <div data-testid="sidebar-node-detail">
                <SectionLabel>{t("graphs.nodeDetail")}</SectionLabel>
                <NodeDetailPanel
                  nodeName={selectedParsedNode.name}
                  nodeType={selectedParsedNode.nodeType}
                  pool={selectedParsedNode.config.pool}
                  status={selectedVisualStatus}
                  statusLabel={t(statusLabelKey(selectedVisualStatus))}
                  nodeId={selectedInstanceNode?.node_id}
                  result={selectedInstanceNode?.result}
                  isAgent={selectedParsedNode.nodeType === "agent"}
                  onOpenSession={(): void =>
                    handleOpenSession(selectedParsedNode.name)
                  }
                />
              </div>
            ) : (
              <div data-testid="sidebar-instance-summary">
                <SectionLabel>{t("graphs.instanceSummary")}</SectionLabel>
                {specInfo ? (
                  <InstanceSummary
                    specName={specInfo.name}
                    specVersion={specInfo.version}
                    scheduler={topology?.scheduler ?? "linear"}
                    triggerMode={topology?.defaultTrigger ?? "on_all_preds"}
                    completedCount={completedCount}
                    totalNodes={totalNodes}
                    elapsedSeconds={elapsedSeconds}
                    isCompleted={status === "completed"}
                    result={instance?.result ?? null}
                  />
                ) : (
                  <p className="text-base text-mute">{t("graphs.loading")}</p>
                )}
              </div>
            )}
          </div>

          {/* Bottom: event timeline */}
          <div className="max-h-[35%] overflow-y-auto border-t border-hairline">
            <EventTimeline events={timeline} />
          </div>

          {/* Inline deliver panel (shown when running/paused) */}
          {canDeliver ? (
            <div
              className="border-t border-hairline p-3"
              data-testid="deliver-inline-panel"
            >
              <SectionLabel>{t("graphs.deliverInline")}</SectionLabel>
              <div className="mt-2 flex flex-col gap-2">
                <DropdownPanel
                  options={deliverNodeNames.map((n) => ({ value: n, label: n }))}
                  value={deliverNodeName}
                  onChange={setDeliverNodeName}
                  label={t("graphs.deliverNodeLabel")}
                  listboxLabel={t("graphs.deliverNodeLabel")}
                />
                <Textarea
                  value={deliverContent}
                  onChange={(e): void => setDeliverContent(e.target.value)}
                  placeholder={t("graphs.deliverContentPlaceholder")}
                  rows={2}
                />
                <Button
                  variant="primary"
                  size="sm"
                  onClick={handleDeliverInline}
                  disabled={!deliverNodeName || !deliverContent.trim() || controlBusy}
                  loading={controlBusy}
                  className="gap-1.5 self-end"
                >
                  <Send size={14} />
                  {t("graphs.deliverConfirm")}
                </Button>
              </div>
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
};
