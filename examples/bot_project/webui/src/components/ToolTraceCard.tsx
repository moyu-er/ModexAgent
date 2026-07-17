import { useState, type FC } from "react";
import { Wrench } from "lucide-react";
import type { ToolTrace } from "../types/events";
import { ChevronToggleIcon } from "./ui/icons";
import { useT } from "../i18n";

export interface ToolTraceCardProps {
  tool: ToolTrace;
}

export const ToolTraceCard: FC<ToolTraceCardProps> = ({ tool }) => {
  const t = useT();
  const [expanded, setExpanded] = useState(false);

  const toggle = (): void => {
    setExpanded((prev) => !prev);
  };

  const argsStr = JSON.stringify(tool.args, null, 2);

  return (
    <div className="mb-1">
      <button
        type="button"
        onClick={toggle}
        className="flex w-full items-center gap-1 text-left text-xs font-medium text-mute transition-colors hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-link/50"
      >
        <ChevronToggleIcon open={expanded} />
        <Wrench size={13} strokeWidth={1.75} className="mr-1" aria-hidden="true" />
        <span className="font-mono">{tool.tool}</span>
        {tool.result !== undefined && (
          <span className="ml-auto text-[10px] text-success">{t("toolTrace.done")}</span>
        )}
      </button>
      {expanded && (
        <div className="mt-1.5 ml-5 rounded border border-hairline bg-canvas p-3">
          <div className="mb-2">
            <span className="text-[10px] font-semibold uppercase text-mute">
              {t("toolTrace.args")}
            </span>
            <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-body">
              {argsStr}
            </pre>
          </div>
          {tool.result !== undefined && (
            <div>
              <span className="text-[10px] font-semibold uppercase text-mute">
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
