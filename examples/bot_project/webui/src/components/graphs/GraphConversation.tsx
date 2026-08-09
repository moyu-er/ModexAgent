// GraphConversation — conversation-style graph view (PRD §6.4).
//
// Replaces the YAML-editor-first entry point for /graphs/:id with a chat-like
// I/O history: each past run renders as a user-input bubble (right) + graph
// output surface (left), and a composer at the bottom fires new runs via
// runGraph. Polls getRuns every 2s while a run is active.

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FC,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { ArrowLeft, Code2, ExternalLink, ListTree, SendHorizonal } from "lucide-react";
import {
  getRuns,
  getSpec,
  getInstance,
  runGraph,
  type GraphRunRecord,
  type GraphNodeStatus,
} from "../../lib/graphsApi";
import type { WebSocketClient } from "../../lib/ws-client";
import { useT } from "../../i18n";
import { formatClock } from "../../lib/timezone";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { MarkdownRenderer } from "../MarkdownRenderer";
import { GraphStatusBadge, formatGraphApiError } from "./shared";
import { statusLabelKey } from "./GraphExecutionViewer";
import { mergeGraphOutput } from "./detail/mergeOutput";
import { MiniTopology } from "./topology/MiniTopology";
import type { GraphNodeVisualStatus } from "./topology/GraphNode";
import {
  GRAPH_NODE_END,
  GRAPH_NODE_START,
  parseGraphSpecYaml,
  type ParsedGraphTopology,
} from "./yaml/parseGraphSpec";

const MAX_INPUT_HEIGHT = 320;
const MIN_INPUT_HEIGHT = 56;
const POLL_INTERVAL_MS = 2000;

const TERMINAL_STATUSES = new Set(["completed", "crashed", "stopped", "failed"]);
const ACTIVE_STATUSES = new Set(["pending", "running"]);

const CONTENT_WIDTH = "mx-auto w-full min-w-0 max-w-[1200px] md:min-w-[720px]";

export interface GraphConversationProps {
  workspaceId: string;
  specId: string;
  /** Live WS client — reserved for event-driven run updates (polling is the
   *  current implementation). Undefined in tests or before WS connects. */
  wsClient?: WebSocketClient;
  onBack: () => void;
  onOpenInstance: (instanceId: string) => void;
  onEditYaml: () => void;
  onOpenInstances: () => void;
}

export const GraphConversation: FC<GraphConversationProps> = ({
  workspaceId,
  specId,
  onBack,
  onOpenInstance,
  onEditYaml,
  onOpenInstances,
}) => {
  const t = useT();
  const [specInfo, setSpecInfo] = useState<{
    name: string;
    version: string;
  } | null>(null);
  const [topology, setTopology] = useState<ParsedGraphTopology | null>(null);
  const [runs, setRuns] = useState<GraphRunRecord[]>([]);
  const [input, setInput] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [pollRevision, setPollRevision] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  const refreshRuns = useCallback((): void => {
    getRuns(workspaceId, specId)
      .then((fetched) => {
        setRuns(fetched);
        setPollRevision((r) => r + 1);
        const latest = fetched[fetched.length - 1];
        if (latest && TERMINAL_STATUSES.has(latest.status)) {
          setIsRunning(false);
        }
      })
      .catch(() => {
        // Polling errors are transient — the next tick retries.
      });
  }, [workspaceId, specId]);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    setError(null);
    setSpecInfo(null);
    setTopology(null);
    setRuns([]);
    setIsRunning(false);
    Promise.all([getSpec(workspaceId, specId), getRuns(workspaceId, specId)])
      .then(([spec, loadedRuns]) => {
        if (cancelled) return;
        setSpecInfo({ name: spec.name, version: spec.version });
        try {
          setTopology(parseGraphSpecYaml(spec.yaml_content));
        } catch {
          setTopology(null);
        }
        setRuns(loadedRuns);
        const latest = loadedRuns[loadedRuns.length - 1];
        if (latest && ACTIVE_STATUSES.has(latest.status)) {
          setIsRunning(true);
        }
      })
      .catch((err) => {
        if (!cancelled) setError(formatGraphApiError(err));
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId, specId]);

  useEffect(() => {
    if (!isRunning) return;
    const timer = setInterval(refreshRuns, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [isRunning, refreshRuns]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runs]);

  const autosize = (): void => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = `${Math.max(
      MIN_INPUT_HEIGHT,
      Math.min(ta.scrollHeight, MAX_INPUT_HEIGHT),
    )}px`;
  };
  useEffect(() => {
    autosize();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [input]);

  const handleSend = (): void => {
    if (isRunning || !input.trim()) return;
    const content = input.trim();
    setInput("");
    setIsRunning(true);
    const optimisticId = `optimistic-${Date.now()}`;
    setRuns((prev) => [
      ...prev,
      {
        record_id: optimisticId,
        graph_instance_id: "",
        user_input: { content },
        output: null,
        status: "pending",
        created_at: Date.now(),
        updated_at: Date.now(),
      },
    ]);
    runGraph(workspaceId, specId, content)
      .then(() => {
        refreshRuns();
      })
      .catch((err) => {
        setRuns((prev) => prev.filter((r) => r.record_id !== optimisticId));
        setError(formatGraphApiError(err));
        setIsRunning(false);
      });
  };

  const handleSubmit = (e: FormEvent<HTMLFormElement>): void => {
    e.preventDefault();
    handleSend();
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): void => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      handleSend();
    }
  };

  const renderComposer = (): ReactNode => {
    const disabled = isRunning;
    const placeholder = isRunning
      ? t("graphs.inputDisabledRunning")
      : t("graphs.inputPlaceholder");
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
            rows={1}
            disabled={disabled}
            className="max-h-[320px] min-h-[56px] w-full resize-none overflow-y-auto bg-transparent py-3.5 text-md leading-relaxed text-ink outline-none placeholder:text-faint disabled:opacity-50"
          />
        </div>
        {disabled ? (
          <IconButton
            icon={<SendHorizonal size={18} />}
            label={t("graphs.sendRun")}
            variant="ghost"
            size="md"
            disabled
          />
        ) : (
          <IconButton
            icon={<SendHorizonal size={18} />}
            label={t("graphs.sendRun")}
            variant="primary"
            size="md"
            disabled={!input.trim()}
            onClick={handleSend}
          />
        )}
      </form>
    );
  };

  return (
    <div className="flex flex-1 flex-col bg-canvas" data-testid="graph-conversation">
      <header className="flex h-14 shrink-0 items-center justify-between border-b border-hairline px-4">
        <div className="flex items-center gap-3">
          <Button
            variant="ghost"
            size="sm"
            onClick={onBack}
            className="gap-1.5 -ml-2"
          >
            <ArrowLeft size={14} />
            {t("graphs.back")}
          </Button>
          {specInfo && (
            <>
              <span className="text-base font-medium text-ink">
                {specInfo.name}
              </span>
              <span className="font-mono text-xs text-faint">
                v{specInfo.version}
              </span>
            </>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={onEditYaml}>
            <Code2 size={14} />
            {t("graphs.editYaml")}
          </Button>
          <Button variant="ghost" size="sm" onClick={onOpenInstances}>
            <ListTree size={14} />
            {t("graphs.instances")}
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
          <div className={`${CONTENT_WIDTH} px-2 py-6 md:px-3`}>
            {isLoading ? (
              <p className="text-base text-mute">{t("graphs.loading")}</p>
            ) : runs.length === 0 ? (
              <p className="text-base text-mute">{t("graphs.noRuns")}</p>
            ) : (
              runs.map((run) => (
                <RunEntry
                  key={run.record_id}
                  run={run}
                  topology={topology}
                  workspaceId={workspaceId}
                  pollRevision={pollRevision}
                  onOpenInstance={onOpenInstance}
                />
              ))
            )}
          </div>
        </div>
      </div>

      <div
        className="px-3 pb-6 pt-2 md:px-5"
        style={{ paddingBottom: "max(env(safe-area-inset-bottom, 0px), 1.5rem)" }}
      >
        <div className={CONTENT_WIDTH}>{renderComposer()}</div>
      </div>
    </div>
  );
};

interface RunEntryProps {
  run: GraphRunRecord;
  topology: ParsedGraphTopology | null;
  workspaceId: string;
  pollRevision: number;
  onOpenInstance: (instanceId: string) => void;
}

/** Map backend node status string → GraphNodeVisualStatus for MiniTopology. */
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

const RunEntry: FC<RunEntryProps> = ({
  run,
  topology,
  workspaceId,
  pollRevision,
  onOpenInstance,
}) => {
  const t = useT();
  const [nodeStatuses, setNodeStatuses] = useState<GraphNodeStatus[] | null>(
    null,
  );

  const userInput = run.user_input?.content ?? null;
  const output = mergeGraphOutput(run.output);
  const timeStr = formatClock(run.created_at);
  const isActive = ACTIVE_STATUSES.has(run.status);
  const isCrashed = run.status === "crashed" || run.status === "failed";

  useEffect(() => {
    if (!isActive || !run.graph_instance_id) return;
    let cancelled = false;
    getInstance(workspaceId, run.graph_instance_id)
      .then((inst) => {
        if (!cancelled) setNodeStatuses(inst.nodes);
      })
      .catch(() => {
        // Instance may not exist yet (optimistic run) — leave nodeStatuses null.
      });
    return () => {
      cancelled = true;
    };
    // pollRevision is a poll-cycle trigger (not read in body) — re-fetches getInstance on every 2s refresh, even when updated_at is unchanged.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isActive, run.graph_instance_id, workspaceId, pollRevision]);

  const statusMap = nodeStatuses ? buildNodeStatusMap(nodeStatuses) : undefined;
  const totalNodes = topology ? functionalNodeCount(topology) : 0;
  const completedCount = nodeStatuses
    ? nodeStatuses.filter(
        (n) =>
          n.status === "completed" &&
          n.node_name !== GRAPH_NODE_START &&
          n.node_name !== GRAPH_NODE_END,
      ).length
    : 0;

  const clickable = Boolean(run.graph_instance_id);

  return (
    <div
      className="mb-6 flex w-full flex-col gap-2"
      {...(clickable
        ? {
            role: "button" as const,
            tabIndex: 0,
            onClick: (): void => onOpenInstance(run.graph_instance_id),
            onKeyDown: (e: KeyboardEvent<HTMLDivElement>): void => {
              if (e.key === "Enter") {
                e.preventDefault();
                onOpenInstance(run.graph_instance_id);
              }
            },
          }
        : {})}
    >
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
          {run.status === "completed" ? (
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
                  nodeStatuses={statusMap}
                  className="shrink-0"
                />
              )}
              <div className="flex flex-col gap-1">
                <GraphStatusBadge
                  status={run.status}
                  label={t(statusLabelKey(run.status))}
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
                status={run.status}
                label={t(statusLabelKey(run.status))}
              />
            </div>
          ) : (
            <div className="flex items-center gap-2">
              <GraphStatusBadge
                status={run.status}
                label={t(statusLabelKey(run.status))}
              />
              <span className="text-sm text-mute">
                {t(statusLabelKey(run.status))}
              </span>
            </div>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2 px-1">
        <span className="text-xs text-mute">{timeStr}</span>
        {run.graph_instance_id && (
          <span className="inline-flex items-center gap-1 text-xs text-brand">
            <ExternalLink size={14} />
            {t("graphs.viewExecution")}
          </span>
        )}
      </div>
    </div>
  );
};
