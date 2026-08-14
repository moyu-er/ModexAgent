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
import type { GraphTimelineEvent } from "../../../hooks/useGraphExecution.diff";

/** kind → dot fill class (§5.2 status color system). */
const KIND_DOT_CLS: Readonly<Record<string, string>> = {
  node_started: "fill-brand",
  node_completed: "fill-success",
  node_crashed: "fill-danger",
  graph_completed: "fill-success",
  graph_crashed: "fill-danger",
};

function dotCls(kind: string): string {
  return KIND_DOT_CLS[kind] ?? "fill-mute";
}

interface TimelineRowProps {
  event: GraphTimelineEvent;
  inferredLabel: string;
}

const TimelineRow: FC<TimelineRowProps> = ({ event, inferredLabel }) => {
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
          className="flex items-center gap-1.5 text-left disabled:cursor-default"
        >
          <span className="font-mono text-xs text-body">{event.kind}</span>
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
