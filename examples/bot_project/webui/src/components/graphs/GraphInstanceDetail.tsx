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
import { ArrowLeft, ChevronDown, Play, X } from "lucide-react";
import {
  getInvocations,
  getSpec,
  invokeInstance,
  type GraphInvocationRecord,
  type GraphNodeStatus,
} from "../../lib/graphsApi";
import type { WebSocketClient } from "../../lib/ws-client";
import { useT } from "../../i18n";
import { formatClock } from "../../lib/timezone";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { MarkdownRenderer } from "../MarkdownRenderer";
import { GraphStatusBadge, formatGraphApiError, statusLabelKey } from "./shared";
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
  parseGraphSpecYaml,
  type ParsedGraphTopology,
} from "./yaml/parseGraphSpec";
import { useGraphExecution } from "../../hooks/useGraphExecution";

const MAX_INPUT_HEIGHT = 320;
const MIN_INPUT_HEIGHT = 56;

const TERMINAL_STATUSES = new Set(["completed", "crashed", "stopped", "failed"]);
const ACTIVE_STATUSES = new Set(["pending", "running", "paused"]);

const CONTENT_WIDTH = "mx-auto w-full min-w-0 max-w-[800px]";

function toVisualStatus(status: string): GraphNodeVisualStatus {
  switch (status) {
    case "running":
      return "running";
    case "completed":
      return "completed";
    case "crashed":
      return "crashed";
    case "canceled":
    case "cancelled":
      return "canceled";
    case "suspended":
      return "suspended";
    default:
      return "pending";
  }
}

function buildNodeStatusMap(
  nodes: GraphNodeStatus[],
): Record<string, GraphNodeVisualStatus> {
  const map: Record<string, GraphNodeVisualStatus> = {};
  for (const n of nodes) {
    map[n.node_name] = toVisualStatus(n.status);
  }
  return map;
}

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
  const [drawerOpen, setDrawerOpen] = useState(false);
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
    pulses,
    error: pollError,
    refresh,
    dismissPulse,
  } = useGraphExecution(workspaceId, instanceId, edges, wsClient);

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
    getSpec(workspaceId, specId)
      .then((spec) => {
        if (cancelled) return;
        setSpecInfo({ name: spec.name, version: spec.version });
        try {
          setTopology(parseGraphSpecYaml(spec.yaml_content));
        } catch {
          setTopology(null);
        }
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

  // Esc key closes drawer.
  useEffect(() => {
    if (!drawerOpen) return;
    const onEsc = (e: globalThis.KeyboardEvent): void => {
      if (e.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onEsc);
    return () => window.removeEventListener("keydown", onEsc);
  }, [drawerOpen]);

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
  const error = loadError ?? pollError;

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
      .then(() => {
        refreshInvocations();
        refresh();
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
            onClick={() => setDrawerOpen((v) => !v)}
          >
            {t("graphs.topology")}
            <ChevronDown size={14} />
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

      {drawerOpen && (
        <TopologyDrawer
          topology={topology}
          nodeStatuses={nodeStatuses}
          activeEdges={activeEdges}
          pulses={canvasPulses}
          onPulseComplete={dismissPulse}
          onOpenSession={handleOpenSession}
          specInfo={specInfo}
          scheduler={topology?.scheduler ?? ""}
          triggerMode={topology?.defaultTrigger ?? ""}
          totalNodes={totalNodes}
          onClose={() => setDrawerOpen(false)}
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

interface TopologyDrawerProps {
  topology: ParsedGraphTopology | null;
  nodeStatuses: Record<string, GraphNodeVisualStatus>;
  activeEdges: Set<string>;
  pulses: CanvasPulseSignal[];
  onPulseComplete: (id: number) => void;
  onOpenSession: (nodeName: string) => void;
  specInfo: { name: string; version: string } | null;
  scheduler: string;
  triggerMode: string;
  totalNodes: number;
  onClose: () => void;
}

const TopologyDrawer: FC<TopologyDrawerProps> = ({
  topology,
  nodeStatuses,
  activeEdges,
  pulses,
  onPulseComplete,
  onOpenSession,
  specInfo,
  scheduler,
  triggerMode,
  totalNodes,
  onClose,
}) => {
  const t = useT();
  return (
    <div
      className="absolute right-0 top-0 bottom-0 z-50 flex w-[360px] flex-col border-l border-border-strong bg-canvas-popover shadow-card-hover"
      data-testid="topology-drawer"
      role="dialog"
      aria-label={t("graphs.drawerTitle")}
    >
      <div className="flex items-center justify-between border-b border-hairline px-4 py-3">
        <span className="font-mono text-xs uppercase tracking-widest text-brand">
          {t("graphs.drawerTitle")}
          {specInfo && ` · ${t("graphs.specVersion", { version: specInfo.version })}`}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label={t("common.close")}
          className="rounded-sm p-1 text-mute hover:bg-hairline-soft focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
        >
          <X size={18} />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-4">
        {topology ? (
          <TopologyCanvas
            topology={topology}
            nodeStatuses={nodeStatuses}
            activeEdges={activeEdges}
            pulses={pulses}
            onPulseComplete={onPulseComplete}
            onOpenSession={onOpenSession}
            className="min-h-[300px]"
          />
        ) : (
          <p className="text-base text-mute">{t("graphs.loading")}</p>
        )}
        <div className="mt-4 flex flex-col gap-1 font-mono text-xs">
          <div className="flex justify-between">
            <span className="text-faint">{t("graphs.scheduler")}</span>
            <span className="text-body">{scheduler}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-faint">{t("graphs.triggerMode")}</span>
            <span className="text-body">{triggerMode}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-faint">{t("graphs.nodes")}</span>
            <span className="text-body">{totalNodes}</span>
          </div>
          {specInfo && (
              <div className="flex justify-between">
              <span className="text-faint">{t("graphs.versionLabel")}</span>
              <span className="text-body">{t("graphs.version", { version: specInfo.version })}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
