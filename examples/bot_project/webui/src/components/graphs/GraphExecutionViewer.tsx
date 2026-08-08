// GraphExecutionViewer — live state of one graph instance. Polls
// GET /instances/{id} (+ /events) while the instance is non-terminal, offers
// pause/resume/stop controls, and jumps from an agent node to its session
// transcript (session_id = "{node_id}.{node_name}", matching
// AgentNode._ensure_session's f"{node_id}.{agent_name}").

import { useCallback, useEffect, useState, type FC } from "react";
import { ArrowLeft, ExternalLink, Pause, Play, Square } from "lucide-react";
import {
  getEvents,
  getInstance,
  pauseGraph,
  resumeGraph,
  stopGraph,
  type GraphEvent,
  type GraphInstance,
} from "../../lib/graphsApi";
import { useT, type MessageKey } from "../../i18n";
import { Button } from "../ui/Button";
import { Card } from "../ui/Card";
import { SectionLabel } from "../ui/SectionLabel";
import { formatGraphApiError, GraphStatusBadge } from "./shared";

// crashed stays polled — fault recovery may auto-resume it to running.
const ACTIVE_STATUSES = new Set(["pending", "running", "paused", "crashed"]);
const POLL_MS = 2000;

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

export interface GraphExecutionViewerProps {
  workspaceId: string;
  instanceId: string;
  onBack: () => void;
  onJumpToSession: (sessionId: string) => void;
}

export const GraphExecutionViewer: FC<GraphExecutionViewerProps> = ({
  workspaceId,
  instanceId,
  onBack,
  onJumpToSession,
}) => {
  const t = useT();
  const [instance, setInstance] = useState<GraphInstance | null>(null);
  const [events, setEvents] = useState<{ key: string; event: GraphEvent }[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [controlBusy, setControlBusy] = useState(false);

  const refresh = useCallback((): void => {
    getInstance(workspaceId, instanceId)
      .then(setInstance)
      .catch((err) => setError(formatGraphApiError(err)));
    getEvents(workspaceId, instanceId)
      .then((loaded) => {
        // No id/timestamp on GraphOutput — key by per-kind ordinal, stable
        // across polls because the backend event list is append-only.
        const counts = new Map<string, number>();
        setEvents(
          loaded.map((event) => {
            const ordinal = counts.get(event.kind) ?? 0;
            counts.set(event.kind, ordinal + 1);
            return { key: `${event.kind}-${ordinal}`, event };
          }),
        );
      })
      .catch(() => {
        // Event polling is best-effort; the instance payload carries status.
      });
  }, [workspaceId, instanceId]);

  // Poll while the instance is (or may become) active; terminal instances
  // render one final snapshot and stop the interval.
  useEffect(() => {
    setInstance(null);
    setEvents([]);
    setError(null);
    refresh();
    const timer = setInterval(() => {
      setInstance((current) => {
        if (current && !ACTIVE_STATUSES.has(current.status)) {
          clearInterval(timer);
          return current;
        }
        refresh();
        return current;
      });
    }, POLL_MS);
    return (): void => clearInterval(timer);
  }, [refresh]);

  const control = useCallback(
    (fn: typeof pauseGraph): void => {
      setControlBusy(true);
      setError(null);
      fn(workspaceId, instanceId)
        .then(() => refresh())
        .catch((err) => setError(formatGraphApiError(err)))
        .finally(() => setControlBusy(false));
    },
    [workspaceId, instanceId, refresh],
  );

  const status = instance?.status ?? "";
  const canPause = status === "running" || status === "pending";
  const canResume = status === "paused";
  const canStop = ACTIVE_STATUSES.has(status);

  return (
    <div className="flex-1 overflow-y-auto px-6 py-6">
      <div className="mx-auto flex max-w-3xl flex-col gap-5">
        <div className="flex items-center justify-between">
          <Button variant="ghost" size="sm" onClick={onBack} className="gap-1.5 -ml-2">
            <ArrowLeft size={14} />
            {t("graphs.back")}
          </Button>
          <div className="flex items-center gap-3">
            <span className="text-base font-medium text-ink">
              {t("graphs.instance", { id: instanceId })}
            </span>
            {instance ? (
              <GraphStatusBadge status={status} label={t(statusLabelKey(status))} />
            ) : null}
          </div>
        </div>

        {error ? (
          <pre className="whitespace-pre-wrap rounded-sm border border-danger bg-canvas-elevated px-3 py-2 font-mono text-xs text-danger">
            {error}
          </pre>
        ) : null}

        {instance && (canPause || canResume || canStop) ? (
          <div className="flex items-center gap-2">
            {canPause ? (
              <Button
                variant="secondary"
                size="sm"
                disabled={controlBusy}
                onClick={(): void => control(pauseGraph)}
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
                onClick={(): void => control(resumeGraph)}
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
                onClick={(): void => control(stopGraph)}
              >
                <Square size={14} />
                {t("graphs.stop")}
              </Button>
            ) : null}
          </div>
        ) : null}

        <section>
          <SectionLabel>{t("graphs.nodes")}</SectionLabel>
          {!instance ? (
            <p className="text-base text-mute">{t("graphs.loading")}</p>
          ) : instance.nodes.length === 0 ? (
            <p className="text-base text-mute">{t("graphs.noNodes")}</p>
          ) : (
            <div className="flex flex-col gap-2">
              {instance.nodes.map((node) => (
                <Card key={node.node_id} hoverable className="p-0">
                  <Button
                    variant="ghost"
                    size="md"
                    onClick={(): void =>
                      onJumpToSession(`${node.node_id}.${node.node_name}`)
                    }
                    title={t("graphs.openSession")}
                    className="h-auto w-full justify-between gap-2 rounded-md px-4 py-2.5 text-left hover:bg-hairline-soft"
                  >
                    <span className="flex min-w-0 items-center gap-2.5">
                      <span className="truncate text-base text-ink">{node.node_name}</span>
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
                  </Button>
                </Card>
              ))}
            </div>
          )}
        </section>

        <section>
          <SectionLabel>{t("graphs.events")}</SectionLabel>
          {events.length === 0 ? (
            <p className="text-base text-mute">{t("graphs.noEvents")}</p>
          ) : (
            <div className="flex flex-col gap-2">
              {events.map(({ key, event }) => (
                <Card key={key} className="px-4 py-2.5">
                  <div className="font-mono text-xs text-body">{event.kind}</div>
                  {event.error ? (
                    <div className="mt-1 text-xs text-danger">{event.error}</div>
                  ) : null}
                </Card>
              ))}
            </div>
          )}
        </section>
      </div>
    </div>
  );
};
