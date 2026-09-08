// GraphInstanceDetail — conversation-first instance view (PRD Rev 3 §1.1).
//
// Layout: header + invocation conversation flow + bottom re-invoke composer.
// No run graph is visible by default; the header "Topology" button opens a
// centered near-fullscreen Run Graph modal carrying the full live-graph
// experience (merged from the retired full-page execution viewer):
//   - top bar: spec name · version chip · status badge · Pause/Resume/Stop · ✕
//   - full-size TopologyCanvas (nodeStatuses + activeEdges + pulses + crash flash)
//   - sidebar w-80: NodeDetailPanel (node selected) / InstanceSummary + EventTimeline
//   - inline Deliver panel while running/paused
// The modal traps focus while open and returns focus to the opener on close
// (Esc / ✕ / backdrop click). useGraphExecution stays mounted on the page so
// badges and bubble progress keep updating while the modal is closed.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FC,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { ArrowLeft, Pause, Play, Send, Square, X } from "lucide-react";
import {
  deliverToNode,
  getInvocations,
  getSpec,
  getTopology,
  invokeInstance,
  pauseGraph,
  resumeGraph,
  stopGraph,
  type GraphInstance,
  type GraphInvocationRecord,
} from "../../lib/graphsApi";
import type { WebSocketClient } from "../../lib/ws-client";
import { useT } from "../../i18n";
import { formatClock } from "../../lib/timezone";
import { useToast } from "../ToastContext";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { SectionLabel } from "../ui/SectionLabel";
import { DropdownPanel } from "../ui/DropdownPanel";
import { MarkdownRenderer } from "../MarkdownRenderer";
import {
  buildNodeStatusMap,
  formatGraphApiError,
  GraphStatusBadge,
  statusLabelKey,
} from "./shared";
import { mergeGraphOutput } from "./detail/mergeOutput";
import { MiniTopology } from "./topology/MiniTopology";
import {
  TopologyCanvas,
  type PulseSignal as CanvasPulseSignal,
} from "./topology/TopologyCanvas";
import type { GraphNodeVisualStatus } from "./topology/GraphNode";
import { edgeKey, layoutGraph } from "./topology/layout";
import {
  GRAPH_NODE_END,
  GRAPH_NODE_START,
  type ParsedGraphTopology,
} from "./yaml/parseGraphSpec";
import { topologyFromApi } from "./topologyFromApi";
import { NodeDetailPanel } from "./detail/NodeDetailPanel";
import { InstanceSummary } from "./detail/InstanceSummary";
import { EventTimeline } from "./detail/EventTimeline";
import { useGraphExecution } from "../../hooks/useGraphExecution";
import { useModalFocus } from "../../hooks/useModalFocus";
import type { GraphTimelineEvent } from "../../hooks/useGraphExecution.diff";

const MAX_INPUT_HEIGHT = 320;
const MIN_INPUT_HEIGHT = 56;

const TERMINAL_STATUSES = new Set(["completed", "crashed", "stopped", "failed"]);
const ACTIVE_STATUSES = new Set(["pending", "running", "pausing", "paused", "stopping"]);
// Stoppable statuses mirror the hook's ACTIVE_STATUSES: crashed stays
// stoppable because fault recovery may auto-resume it to running.
const STOPPABLE_STATUSES = new Set(["pending", "running", "paused", "crashed"]);
/** Crash-flash auto-dismiss (PRD §8.1: --dur = 220ms). */
const CRASH_FLASH_MS = 220;

const CONTENT_WIDTH = "mx-auto w-full min-w-0 max-w-[800px]";

function functionalNodeCount(topology: ParsedGraphTopology): number {
  return topology.nodes.filter(
    (n) => n.nodeType !== GRAPH_NODE_START && n.nodeType !== GRAPH_NODE_END,
  ).length;
}

export interface GraphInstanceDetailProps {
  workspaceId: string;
  instanceId: string;
  wsClient?: WebSocketClient;
  onBack: () => void;
  onJumpToSession?: (sessionId: string) => void;
}

export const GraphInstanceDetail: FC<GraphInstanceDetailProps> = ({
  workspaceId,
  instanceId,
  wsClient,
  onBack,
  onJumpToSession,
}) => {
  const t = useT();
  const [topology, setTopology] = useState<ParsedGraphTopology | null>(null);
  const [specInfo, setSpecInfo] = useState<{
    name: string;
    version: string;
  } | null>(null);
  const [invocations, setInvocations] = useState<GraphInvocationRecord[]>([]);
  const [input, setInput] = useState("");
  const [isInvoking, setIsInvoking] = useState(false);
  const [controlBusy, setControlBusy] = useState(false);
  const [controlError, setControlError] = useState<string | null>(null);
  const controlPending = useRef(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [invocationsLoading, setInvocationsLoading] = useState(true);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const prevInstanceStatus = useRef<string>("");

  const edges = useMemo(
    () => topology?.edges ?? [],
    [topology],
  );

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

  const handleControl = useCallback((fn: typeof pauseGraph): void => {
    if (controlPending.current) return;
    controlPending.current = true;
    setControlBusy(true);
    setControlError(null);
    fn(workspaceId, instanceId)
      .then(() => refresh())
      .catch((err) => setControlError(formatGraphApiError(err)))
      .finally(() => {
        controlPending.current = false;
        setControlBusy(false);
      });
  }, [workspaceId, instanceId, refresh]);

  const nodeStatuses = useMemo<Record<string, GraphNodeVisualStatus>>(() => {
    return buildNodeStatusMap(instance?.nodes ?? []);
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

  const canvasPulses = useMemo<CanvasPulseSignal[]>(() => {
    if (!topology) return [];
    const layout = layoutGraph(topology);
    const result: CanvasPulseSignal[] = [];
    for (const pulse of pulses) {
      const key = edgeKey(pulse.edge.source, pulse.edge.target);
      const laid = layout.edges.get(key);
      if (!laid) continue;
      result.push({ id: pulse.id, edgeKey: key, points: laid.points });
    }
    return result;
  }, [pulses, topology]);

  const activeEdges = useMemo(() => {
    const set = new Set<string>();
    for (const pulse of pulses) {
      set.add(edgeKey(pulse.edge.source, pulse.edge.target));
    }
    return set;
  }, [pulses]);

  // Load spec when instance.spec_id becomes available.
  useEffect(() => {
    const specId = instance?.spec_id;
    if (!specId) return;
    let cancelled = false;
    setLoadError(null);
    Promise.all([
      getSpec(workspaceId, specId),
      getTopology(workspaceId, specId),
    ])
      .then(([spec, topo]) => {
        if (cancelled) return;
        setSpecInfo({ name: spec.name, version: spec.version });
        setTopology(topologyFromApi(topo));
      })
      .catch((err) => {
        if (!cancelled) setLoadError(formatGraphApiError(err));
      });
    return () => {
      cancelled = true;
    };
  }, [instance?.spec_id, workspaceId]);

  // Load invocations on mount / instanceId change.
  const refreshInvocations = useCallback((): void => {
    getInvocations(workspaceId, instanceId)
      .then((fetched) => {
        setInvocations(fetched);
      })
      .catch(() => {
        // Transient — next poll/refresh retries.
      })
      .finally(() => setInvocationsLoading(false));
  }, [workspaceId, instanceId]);

  useEffect(() => {
    setInvocations([]);
    setInvocationsLoading(true);
    refreshInvocations();
  }, [refreshInvocations]);

  // Refresh invocations when instance transitions to terminal.
  useEffect(() => {
    const currentStatus = instance?.status ?? "";
    const prevStatus = prevInstanceStatus.current;
    if (
      prevStatus &&
      ACTIVE_STATUSES.has(prevStatus) &&
      TERMINAL_STATUSES.has(currentStatus)
    ) {
      refreshInvocations();
    }
    prevInstanceStatus.current = currentStatus;
  }, [instance?.status, refreshInvocations]);

  // Modal close is a stable callback: the RunGraphModal's focus/keyboard
  // effect depends on it and must not re-run on every poll render.
  const closeModal = useCallback((): void => setModalOpen(false), []);

  const flashTimers = useRef<Map<number, ReturnType<typeof setTimeout>>>(
    new Map(),
  );

  // Auto-dismiss crash flashes after 220ms (PRD §8.1). Each flash is
  // scheduled exactly once. Lives on the always-mounted page so flashes
  // expire whether the Run Graph modal is open or closed — nothing
  // accumulates or replays on the next open.
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

  // Drain any pending crash-flash timers on unmount (separate unmount-only
  // effect: a cleanup on the scheduling effect would cancel live timers on
  // every crashFlashes change).
  useEffect(() => {
    const timers = flashTimers.current;
    return () => {
      for (const timer of timers.values()) clearTimeout(timer);
      timers.clear();
    };
  }, []);

  // The modal only needs the set of currently-flashing node names.
  const crashNodeNames = useMemo<ReadonlySet<string>>(
    () => new Set(crashFlashes.map((flash) => flash.nodeName)),
    [crashFlashes],
  );

  // Scroll to bottom when invocations change.
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [invocations]);

  // Autosize textarea.
  const autosize = useCallback((): void => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.max(
      MIN_INPUT_HEIGHT,
      Math.min(ta.scrollHeight, MAX_INPUT_HEIGHT),
    )}px`;
  }, []);

  useEffect(() => {
    autosize();
  }, [input, autosize]);

  const status = instance?.status ?? "";
  const isRunning = ACTIVE_STATUSES.has(status);
  const error = controlError ?? loadError ?? pollError;

  const handleInvoke = useCallback((): void => {
    if (isInvoking || isRunning || !input.trim()) return;
    const content = input.trim();
    setInput("");
    setIsInvoking(true);
    const optimisticId = `optimistic-${Date.now()}`;
    setInvocations((prev) => [
      ...prev,
      {
        record_id: optimisticId,
        version: prev.length + 1,
        user_input: { content },
        output: null,
        created_at: Date.now(),
      },
    ]);
    invokeInstance(workspaceId, instanceId, content)
      .then(async () => {
        refreshInvocations();
        await refresh();
      })
      .catch((err) => {
        setInvocations((prev) =>
          prev.filter((r) => r.record_id !== optimisticId),
        );
        setLoadError(formatGraphApiError(err));
      })
      .finally(() => setIsInvoking(false));
  }, [
    isInvoking,
    isRunning,
    input,
    workspaceId,
    instanceId,
    refreshInvocations,
    refresh,
  ]);

  const handleSubmit = (e: FormEvent<HTMLFormElement>): void => {
    e.preventDefault();
    handleInvoke();
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleInvoke();
    }
  };

  const handleOpenSession = useCallback(
    (nodeName: string): void => {
      const node = instance?.nodes.find((n) => n.node_name === nodeName);
      if (node?.session_id) {
        onJumpToSession?.(node.session_id);
      }
    },
    [instance?.nodes, onJumpToSession],
  );

  const renderComposer = (): ReactNode => {
    const disabled = isRunning || isInvoking;
    const placeholder = isRunning
      ? t("graphs.invokeDisabledRunning")
      : t("graphs.reInvokePlaceholder");
    return (
      <form className="composer" onSubmit={handleSubmit}>
        <div className="relative min-w-0 flex-1">
          <textarea
            ref={taRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onInput={autosize}
            placeholder={placeholder}
            rows={2}
            disabled={disabled}
            className="max-h-[320px] min-h-[56px] w-full resize-none overflow-y-auto bg-transparent py-3.5 text-md leading-relaxed text-ink outline-none placeholder:text-faint disabled:opacity-50"
          />
        </div>
        <IconButton
          icon={<Play size={18} />}
          label={t("graphs.invoke")}
          variant="primary"
          size="md"
          disabled={disabled || !input.trim()}
          onClick={handleInvoke}
        />
      </form>
    );
  };

  return (
    <div
      className="relative flex flex-1 flex-col bg-canvas"
      data-testid="graph-instance-detail"
    >
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-hairline px-4">
        <Button
          variant="ghost"
          size="sm"
          onClick={onBack}
          className="gap-1.5 -ml-2"
        >
          <ArrowLeft size={14} />
          {t("graphs.backToSpec")}
        </Button>
        <span className="font-mono text-base text-ink">#{instanceId}</span>
        {specInfo && (
          <>
            <span className="text-sm text-mute">{specInfo.name}</span>
            <span className="inline-flex items-center gap-1 rounded-sm border border-hairline px-1.5 py-0.5 font-mono text-xs text-ember">
              {t("graphs.specVersion", { version: specInfo.version })}
            </span>
          </>
        )}
        {instance && (
          <GraphStatusBadge
            status={status}
            label={t(statusLabelKey(status))}
          />
        )}
        <div className="ml-auto">
          <Button
            variant="secondary"
            size="sm"
            aria-haspopup="dialog"
            onClick={() => setModalOpen(true)}
          >
            {t("graphs.topology")}
          </Button>
        </div>
      </header>

      {error && (
        <pre className="mx-4 mt-3 whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
          {error}
        </pre>
      )}

      <div className="relative flex-1 min-h-0">
        <div ref={scrollRef} className="absolute inset-0 overflow-y-auto">
          <div className={`${CONTENT_WIDTH} px-4 py-6`}>
            {invocationsLoading ? (
              <p className="text-base text-mute">{t("graphs.loading")}</p>
            ) : invocations.length === 0 ? (
              <p className="text-base text-mute">{t("graphs.noInvocations")}</p>
            ) : (
              invocations.map((record, idx) => {
                const isLast = idx === invocations.length - 1;
                const recordStatus = isLast ? status : "completed";
                return (
                  <InvocationEntry
                    key={record.record_id}
                    record={record}
                    status={recordStatus}
                    topology={topology}
                    nodeStatuses={nodeStatuses}
                    totalNodes={totalNodes}
                    completedCount={completedCount}
                  />
                );
              })
            )}
          </div>
        </div>
      </div>

      <div
        className="border-t border-hairline px-4 pb-6 pt-3"
        style={{ paddingBottom: "max(env(safe-area-inset-bottom, 0px), 1.5rem)" }}
      >
        <div className={CONTENT_WIDTH}>{renderComposer()}</div>
      </div>

      {modalOpen && (
        <RunGraphModal
          workspaceId={workspaceId}
          instanceId={instanceId}
          topology={topology}
          specInfo={specInfo}
          instance={instance}
          timeline={timeline}
          nodeStatuses={nodeStatuses}
          activeEdges={activeEdges}
          pulses={canvasPulses}
          crashNodeNames={crashNodeNames}
          completedCount={completedCount}
          totalNodes={totalNodes}
          refresh={refresh}
          controlBusy={controlBusy}
          controlError={controlError ?? pollError}
          onControl={handleControl}
          onPulseComplete={dismissPulse}
          onOpenSession={handleOpenSession}
          onClose={closeModal}
        />
      )}
    </div>
  );
};

interface InvocationEntryProps {
  record: GraphInvocationRecord;
  status: string;
  topology: ParsedGraphTopology | null;
  nodeStatuses: Record<string, GraphNodeVisualStatus>;
  totalNodes: number;
  completedCount: number;
}

const InvocationEntry: FC<InvocationEntryProps> = ({
  record,
  status,
  topology,
  nodeStatuses,
  totalNodes,
  completedCount,
}) => {
  const t = useT();
  const userInput = record.user_input?.content ?? null;
  const output = mergeGraphOutput(record.output);
  const timeStr = formatClock(record.created_at);
  const isActive = ACTIVE_STATUSES.has(status);
  const isCrashed = status === "crashed" || status === "failed";

  return (
    <div className="mb-6 flex w-full flex-col gap-2">
      {userInput !== null && (
        <div className="flex justify-end">
          <div className="bubble-user">
            <div className="whitespace-pre-wrap break-words text-md leading-relaxed">
              {userInput}
            </div>
          </div>
        </div>
      )}
      {userInput === null && (
        <div className="flex justify-end">
          <span className="px-1 text-xs text-faint">{t("graphs.noInput")}</span>
        </div>
      )}

      <div className="flex justify-start">
        <div
          className={`bubble-assistant ${isCrashed ? "border-danger bg-canvas-elevated" : ""}`}
        >
          {status === "completed" ? (
            output ? (
              <MarkdownRenderer content={output} />
            ) : (
              <span className="text-mute">{t("graphs.noOutput")}</span>
            )
          ) : isActive ? (
            <div className="flex items-center gap-3">
              {topology && (
                <MiniTopology
                  topology={topology}
                  nodeStatuses={nodeStatuses}
                  className="shrink-0"
                />
              )}
              <div className="flex flex-col gap-1">
                <GraphStatusBadge
                  status={status}
                  label={t(statusLabelKey(status))}
                />
                {totalNodes > 0 && (
                  <span className="font-mono text-xs text-mute">
                    {t("graphs.progress", {
                      completed: completedCount,
                      total: totalNodes,
                    })}
                  </span>
                )}
              </div>
              <span className="typing-dots" aria-hidden="true">
                <span className="typing-dot" />
                <span className="typing-dot" />
                <span className="typing-dot" />
              </span>
            </div>
          ) : isCrashed ? (
            <div className="flex items-center gap-2 text-danger">
              <GraphStatusBadge
                status={status}
                label={t(statusLabelKey(status))}
              />
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <GraphStatusBadge
                status={status}
                label={t(statusLabelKey(status))}
              />
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 px-1">
        <span className="text-xs text-mute">{timeStr}</span>
      </div>
    </div>
  );
};

interface RunGraphModalProps {
  workspaceId: string;
  instanceId: string;
  topology: ParsedGraphTopology | null;
  specInfo: { name: string; version: string } | null;
  instance: GraphInstance | null;
  timeline: GraphTimelineEvent[];
  nodeStatuses: Record<string, GraphNodeVisualStatus>;
  activeEdges: Set<string>;
  pulses: CanvasPulseSignal[];
  crashNodeNames: ReadonlySet<string>;
  completedCount: number;
  totalNodes: number;
  refresh: () => void;
  controlBusy: boolean;
  controlError: string | null;
  onControl: (fn: typeof pauseGraph) => void;
  onPulseComplete: (id: number) => void;
  onOpenSession: (nodeName: string) => void;
  onClose: () => void;
}

const RunGraphModal: FC<RunGraphModalProps> = ({
  workspaceId,
  instanceId,
  topology,
  specInfo,
  instance,
  timeline,
  nodeStatuses,
  activeEdges,
  pulses,
  crashNodeNames,
  completedCount,
  totalNodes,
  refresh,
  controlBusy: lifecycleBusy,
  controlError,
  onControl,
  onPulseComplete,
  onOpenSession,
  onClose,
}) => {
  const t = useT();
  const toast = useToast();
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [deliverNodeName, setDeliverNodeName] = useState("");
  const [deliverContent, setDeliverContent] = useState("");
  const [deliverBusy, setDeliverBusy] = useState(false);
  const controlBusy = lifecycleBusy || deliverBusy;
  const [actionError, setActionError] = useState<string | null>(null);
  const [runStartTime, setRunStartTime] = useState<number | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const deliverTaRef = useRef<HTMLTextAreaElement | null>(null);

  // Focus management: move focus into the dialog on open, trap Tab inside,
  // restore focus to the opener on close. Esc closes the modal.
  useModalFocus({ dialogRef, onClose });

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

  const handleDeliverInline = useCallback((): void => {
    if (!deliverNodeName || !deliverContent.trim() || controlBusy) return;
    setDeliverBusy(true);
    setActionError(null);
    deliverToNode(workspaceId, instanceId, deliverNodeName, deliverContent)
      .then(() => {
        refresh();
        toast.show({
          message: t("graphs.deliverSuccess", { name: deliverNodeName }),
        });
        setDeliverContent("");
      })
      .catch((err) => setActionError(formatGraphApiError(err)))
      .finally(() => setDeliverBusy(false));
  }, [workspaceId, instanceId, deliverNodeName, deliverContent, controlBusy, refresh, toast, t]);

  const autosizeDeliver = useCallback((): void => {
    const ta = deliverTaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.max(44, Math.min(ta.scrollHeight, 160))}px`;
  }, []);

  useEffect(() => {
    autosizeDeliver();
  }, [deliverContent, autosizeDeliver]);

  const status = instance?.status ?? "";
  const canPause = status === "running" || status === "pending";
  const canResume = status === "paused";
  const canStop = STOPPABLE_STATUSES.has(status);
  const canDeliver = status === "running" || status === "paused";

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

  // Portal at document.body: the modal must escape any ancestor transform
  // (same containment hazard documented in WorkspaceBrowser).
  return createPortal(
    <div
      className="modal-scrim-enter fixed inset-0 z-50 bg-overlay"
      data-testid="run-graph-backdrop"
      onClick={onClose}
      role="presentation"
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-label={t("graphs.drawerTitle")}
        tabIndex={-1}
        data-testid="run-graph-modal"
        className="modal-panel-enter absolute inset-6 flex flex-col overflow-hidden rounded-lg border border-hairline bg-canvas-popover shadow-card-hover focus:outline-none"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Top bar: spec name · version chip · status badge · controls · ✕ */}
        <div
          className="flex shrink-0 items-center gap-3 border-b border-hairline px-4 py-3"
          data-testid="control-bar"
        >
          {specInfo ? (
            <>
              <span className="truncate text-base font-medium text-ink">
                {specInfo.name}
              </span>
              <span className="inline-flex items-center gap-1 rounded-sm border border-hairline px-1.5 py-0.5 font-mono text-xs text-ember">
                {t("graphs.specVersion", { version: specInfo.version })}
              </span>
            </>
          ) : (
            <span className="text-base text-mute">{t("graphs.loading")}</span>
          )}
          {instance && (
            <GraphStatusBadge
              status={status}
              label={t(statusLabelKey(status))}
            />
          )}
          <div className="ml-auto flex items-center gap-2">
            {canPause ? (
              <Button
                variant="secondary"
                size="sm"
                disabled={controlBusy}
                onClick={(): void => onControl(pauseGraph)}
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
                onClick={(): void => onControl(resumeGraph)}
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
                onClick={(): void => onControl(stopGraph)}
              >
                <Square size={14} />
                {t("graphs.stop")}
              </Button>
            ) : null}
            <button
              type="button"
              onClick={onClose}
              aria-label={t("common.close")}
              className="rounded-sm p-1 text-mute hover:bg-hairline-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {controlError || actionError ? (
          <pre className="mx-4 mt-3 whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
            {controlError ?? actionError}
          </pre>
        ) : null}

        {/* Body: canvas + sidebar (row on desktop, stacked on mobile) */}
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto md:flex-row md:overflow-hidden">
          {/* TopologyCanvas enforces its own min-h-[400px]; the wrapper's
              mobile min-height matches it so the canvas can't overflow into
              the sidebar stacked below. */}
          <div className="flex min-h-[400px] flex-1 flex-col md:min-h-0 md:min-w-0">
            {topology ? (
              <TopologyCanvas
                topology={topology}
                nodeStatuses={nodeStatuses}
                activeEdges={activeEdges}
                pulses={pulses}
                crashNodeNames={crashNodeNames}
                onPulseComplete={onPulseComplete}
                selectedNodeId={selectedNodeId}
                onSelectNode={setSelectedNodeId}
                onOpenSession={onOpenSession}
                className="flex-1"
              />
            ) : (
              <div className="flex flex-1 items-center justify-center">
                <p className="text-base text-mute">{t("graphs.loading")}</p>
              </div>
            )}
          </div>

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
                      onOpenSession(selectedParsedNode.name)
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
                    <p className="text-base text-mute">
                      {t("graphs.loading")}
                    </p>
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
                    options={deliverNodeNames.map((n) => ({
                      value: n,
                      label: n,
                    }))}
                    value={deliverNodeName}
                    onChange={setDeliverNodeName}
                    label={t("graphs.deliverNodeLabel")}
                    listboxLabel={t("graphs.deliverNodeLabel")}
                  />
                  <textarea
                    ref={deliverTaRef}
                    value={deliverContent}
                    onChange={(e): void => setDeliverContent(e.target.value)}
                    onInput={autosizeDeliver}
                    placeholder={t("graphs.deliverContentPlaceholder")}
                    rows={1}
                    className="w-full resize-none overflow-y-auto rounded-sm border border-hairline bg-canvas-elevated px-3 py-2 text-base text-ink placeholder:text-faint focus:border-brand focus:ring-2 focus:ring-brand focus:outline-none min-h-[44px] max-h-[160px]"
                  />
                  <IconButton
                    icon={<Send size={18} />}
                    label={t("graphs.deliverConfirm")}
                    variant="primary"
                    size="md"
                    disabled={
                      !deliverNodeName || !deliverContent.trim() || controlBusy
                    }
                    onClick={handleDeliverInline}
                    className="self-end"
                  />
                </div>
              </div>
            ) : null}
          </aside>
        </div>
      </div>
    </div>,
    document.body,
  );
};
