// EventTimeline — sidebar bottom: vertical event timeline (PRD §6.1 D).
//
// Renders the unified timeline (derived + REST events, sorted ascending by
// timestamp) as a vertical list, newest at the bottom. Each entry shows a
// status-colored dot, the event kind (mono), and an expandable result/error
// section when the REST payload carries one. Derived (Phase 1 diff) entries
// are marked "inferred".

import { useState, type FC } from "react";
import { useT } from "../../../i18n";
import { SectionLabel } from "../../ui/SectionLabel";
import { GraphStatusBadge, statusLabelKey } from "../shared";
import type { GraphTimelineEvent } from "../../../hooks/useGraphExecution.diff";

/** kind → dot fill class(§6.2 Rev 4 状态色系 — graph-status token,
 * 与画布节点圆点/图例同色;dark 主题下 --color-success === --color-brand,
 * 不能用 fill-success,否则 node_started 与 node_completed 撞色)。 */
const KIND_DOT_CLS: Readonly<Record<string, string>> = {
  node_started: "fill-graph-status-running",
  node_completed: "fill-graph-status-completed",
  node_crashed: "fill-graph-status-crashed",
  graph_completed: "fill-graph-status-completed",
  graph_crashed: "fill-graph-status-crashed",
  graph_failed: "fill-graph-status-crashed",
};

function dotCls(kind: string): string {
  return KIND_DOT_CLS[kind] ?? "fill-mute";
}

interface TimelineRowProps {
  event: GraphTimelineEvent;
  inferredLabel: string;
}

const TimelineRow: FC<TimelineRowProps> = ({ event, inferredLabel }) => {
  const t = useT();
  const [expanded, setExpanded] = useState(false);
  const hasPayload = Boolean(
    (event.event?.result !== undefined && event.event?.result !== null) ||
      event.event?.error,
  );
  const resultStr = event.event?.result
    ? typeof event.event.result === "string"
      ? event.event.result
      : JSON.stringify(event.event.result, null, 2)
    : null;
  const errorStr = event.event?.error ?? null;

  return (
    <li
      data-testid={`timeline-event-${event.key}`}
      className="relative flex gap-2.5"
    >
      {/* Dot */}
      <div className="flex flex-col items-center pt-1">
        <span
          className={`block h-2 w-2 shrink-0 rounded-full ${dotCls(event.kind)}`}
        />
        {/* Vertical connector line */}
        <span className="mt-1 w-px flex-1 bg-hairline" />
      </div>
      {/* Content */}
      <div className="min-w-0 flex-1 pb-3">
        <button
          type="button"
          disabled={!hasPayload}
          onClick={(): void => setExpanded((v) => !v)}
          className="flex flex-wrap items-center gap-1.5 text-left disabled:cursor-default"
        >
          <span className="font-mono text-xs text-body">{event.kind}</span>
          {event.event?.status ? (
            <GraphStatusBadge status={event.event.status} label={t(statusLabelKey(event.event.status))} />
          ) : null}
          {event.derived ? (
            <span className="font-mono text-xs text-faint">
              ({inferredLabel})
            </span>
          ) : null}
          {event.nodeName ? (
            <span className="truncate font-mono text-xs text-faint">
              {event.nodeName}
            </span>
          ) : null}
        </button>
        {expanded && (resultStr || errorStr) ? (
          <div className="mt-1 flex flex-col gap-1">
            {resultStr ? (
              <pre className="whitespace-pre-wrap break-words rounded-sm border border-hairline bg-canvas px-2 py-1 font-mono text-xs text-body">
                {resultStr}
              </pre>
            ) : null}
            {errorStr ? (
              <pre className="whitespace-pre-wrap break-words rounded-sm border border-danger bg-canvas px-2 py-1 font-mono text-xs text-danger">
                {errorStr}
              </pre>
            ) : null}
          </div>
        ) : null}
      </div>
    </li>
  );
};

export interface EventTimelineProps {
  events: GraphTimelineEvent[];
}

export const EventTimeline: FC<EventTimelineProps> = ({ events }) => {
  const t = useT();
  return (
    <div data-testid="event-timeline" className="p-4">
      <SectionLabel>{t("graphs.events")}</SectionLabel>
      {events.length === 0 ? (
        <p className="text-xs text-faint">{t("graphs.noEvents")}</p>
      ) : (
        <ul className="flex flex-col">
          {events.map((event) => (
            <TimelineRow
              key={event.key}
              event={event}
              inferredLabel={t("graphs.inferred")}
            />
          ))}
        </ul>
      )}
    </div>
  );
};
