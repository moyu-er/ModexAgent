// InstanceSummary — sidebar panel shown when no node is selected (PRD §6.1 D).
//
// Shows spec name + version, scheduler + trigger mode, an SVG circular
// progress ring (completed/total), elapsed time, and the graph-level result
// (from instance.result, shown when the instance is completed — Phase 1).

import type { FC } from "react";
import { useT } from "../../../i18n";
import { SectionLabel } from "../../ui/SectionLabel";
import type { GraphPayload } from "../../../lib/graphsApi";
import { mergeGraphOutput } from "./mergeOutput";

export interface InstanceSummaryProps {
  specName: string;
  specVersion: string;
  scheduler: string;
  triggerMode: string;
  completedCount: number;
  totalNodes: number;
  elapsedSeconds: number;
  isCompleted: boolean;
  result: GraphPayload[] | null;
}

// Progress ring SVG geometry.
const RING_R = 22;
const RING_C = 2 * Math.PI * RING_R;
const RING_SIZE = 56;

function ProgressRing({
  completed,
  total,
}: {
  completed: number;
  total: number;
}): React.ReactElement {
  const fraction = total > 0 ? completed / total : 0;
  const dash = RING_C * fraction;
  return (
    <svg
      width={RING_SIZE}
      height={RING_SIZE}
      viewBox={`0 0 ${RING_SIZE} ${RING_SIZE}`}
      data-testid="progress-ring"
    >
      <circle
        cx={RING_SIZE / 2}
        cy={RING_SIZE / 2}
        r={RING_R}
        fill="none"
        strokeWidth={4}
        className="stroke-hairline"
      />
      <circle
        cx={RING_SIZE / 2}
        cy={RING_SIZE / 2}
        r={RING_R}
        fill="none"
        strokeWidth={4}
        stroke="var(--color-brand)"
        strokeLinecap="round"
        strokeDasharray={`${dash} ${RING_C}`}
        transform={`rotate(-90 ${RING_SIZE / 2} ${RING_SIZE / 2})`}
        className="transition-[stroke-dasharray] duration-app ease-out"
      />
      <text
        x={RING_SIZE / 2}
        y={RING_SIZE / 2}
        dominantBaseline="central"
        textAnchor="middle"
        className="fill-current font-mono text-xs text-ink"
      >
        {completed}/{total}
      </text>
    </svg>
  );
}

function ResultDisplay({
  result,
  noResultLabel,
  resultLabel,
}: {
  result: GraphPayload[] | null;
  noResultLabel: string;
  resultLabel: string;
}): React.ReactElement | null {
  const merged = mergeGraphOutput(result);
  if (merged === "") {
    return (
      <div>
        <SectionLabel>{resultLabel}</SectionLabel>
        <p className="text-xs text-faint">{noResultLabel}</p>
      </div>
    );
  }
  return (
    <div>
      <SectionLabel>{resultLabel}</SectionLabel>
      <pre
        className="whitespace-pre-wrap break-words rounded-sm border border-hairline bg-canvas px-2 py-1.5 font-mono text-xs text-body"
        data-testid="graph-result-item"
      >
        {merged}
      </pre>
    </div>
  );
}

export const InstanceSummary: FC<InstanceSummaryProps> = ({
  specName,
  specVersion,
  scheduler,
  triggerMode,
  completedCount,
  totalNodes,
  elapsedSeconds,
  isCompleted,
  result,
}) => {
  const t = useT();
  return (
    <div
      data-testid="instance-summary"
      className="flex flex-col gap-4"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-base font-medium text-ink">
            {specName}
          </div>
          <div className="font-mono text-xs text-faint">
            {t("graphs.version", { version: specVersion })}
          </div>
        </div>
        <ProgressRing completed={completedCount} total={totalNodes} />
      </div>
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1.5 font-mono text-xs text-faint">
          <span>{t("graphs.scheduler")}:</span>
          <span className="text-body">{scheduler}</span>
        </div>
        <div className="flex items-center gap-1.5 font-mono text-xs text-faint">
          <span>{t("graphs.triggerMode")}:</span>
          <span className="text-body">{triggerMode}</span>
        </div>
        <div className="flex items-center gap-1.5 font-mono text-xs text-faint">
          <span>{t("graphs.elapsed", { seconds: elapsedSeconds })}</span>
        </div>
      </div>
      {isCompleted ? (
        <ResultDisplay
          result={result}
          noResultLabel={t("graphs.noResult")}
          resultLabel={t("graphs.resultLabel")}
        />
      ) : null}
    </div>
  );
};
