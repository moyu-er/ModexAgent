import { useState, type FC, type CSSProperties } from "react";
import { Check, Wrench } from "lucide-react";
import type { ToolTrace } from "../types/events";
import { ChevronToggleIcon } from "./ui/icons";
import { useT } from "../i18n";

export interface ToolTraceCardProps {
  tool: ToolTrace;
}

// Tool traces are unclassified — the shared trace-card severity bar stays at
// the normal (mute) level.
const NORMAL_SEV = { "--sev": "var(--color-severity-normal)" } as CSSProperties;

/** Tool trace card (§6): shares the trace-card language — elevated surface,
 *  mono eyebrow header, 3px severity left bar. Completion is signalled by a
 *  check icon + text label, never by color alone. */
export const ToolTraceCard: FC<ToolTraceCardProps> = ({ tool }) => {
  const t = useT();
  const [expanded, setExpanded] = useState(false);

  const toggle = (): void => {
    setExpanded((prev) => !prev);
  };

  const argsStr = JSON.stringify(tool.args, null, 2);

  return (
    <div className="trace-card mb-1 px-3 py-2" style={NORMAL_SEV}>
      <button
        type="button"
        onClick={toggle}
        aria-expanded={expanded}
        className="flex w-full items-center gap-1.5 text-left transition-colors hover:brightness-125 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
      >
        <ChevronToggleIcon open={expanded} className="text-mute" />
        <Wrench size={13} strokeWidth={1.75} className="text-mute" aria-hidden="true" />
        <span className="eyebrow">{tool.tool}</span>
        {tool.result !== undefined && (
          <span className="ml-auto flex items-center gap-1 text-xs text-success">
            <Check size={11} strokeWidth={2.5} aria-hidden="true" />
            {t("toolTrace.done")}
          </span>
        )}
      </button>
      {expanded && (
        <div className="mt-1.5 rounded border border-hairline bg-canvas p-3">
          <div className="mb-2">
            <span className="text-xs font-semibold uppercase text-mute">
              {t("toolTrace.args")}
            </span>
            <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-body">
              {argsStr}
            </pre>
          </div>
          {tool.result !== undefined && (
            <div>
              <span className="text-xs font-semibold uppercase text-mute">
                {t("toolTrace.result")}
              </span>
              <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-ink">
                {tool.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
