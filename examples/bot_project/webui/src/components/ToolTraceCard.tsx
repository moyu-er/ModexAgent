import { useState, type FC } from "react";
import type { ToolTrace } from "../types/events";
import { ChevronToggleIcon, WrenchIcon } from "./ui/icons";

export interface ToolTraceCardProps {
  tool: ToolTrace;
}

export const ToolTraceCard: FC<ToolTraceCardProps> = ({ tool }) => {
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
        <WrenchIcon className="mr-1" />
        <span className="font-mono">{tool.tool}</span>
        {tool.result !== undefined && (
          <span className="ml-auto text-[10px] text-success">done</span>
        )}
      </button>
      {expanded && (
        <div className="mt-1.5 ml-5 rounded border border-hairline bg-canvas p-3">
          <div className="mb-2">
            <span className="text-[10px] font-semibold uppercase text-mute">
              Args
            </span>
            <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-body">
              {argsStr}
            </pre>
          </div>
          {tool.result !== undefined && (
            <div>
              <span className="text-[10px] font-semibold uppercase text-mute">
                Result
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
