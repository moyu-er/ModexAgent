import { useState, type FC } from "react";
import type { ToolTrace } from "../types/events";

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
        className="flex w-full items-center gap-1 text-left text-xs font-medium text-text-secondary-light transition-colors hover:text-text-primary-light dark:text-text-secondary-dark dark:hover:text-text-primary-dark"
      >
        <span className="inline-block text-[10px] transition-transform duration-150">
          {expanded ? "▼" : "▸"}
        </span>
        <span className="mr-1 text-[11px]">🔧</span>
        <span className="font-mono">{tool.tool}</span>
        {tool.result !== undefined && (
          <span className="ml-auto text-[10px] text-success-light dark:text-success-dark">done</span>
        )}
      </button>
      {expanded && (
        <div className="mt-1.5 ml-5 rounded border border-code-border-light bg-code-bg-light p-3 dark:border-code-border-dark dark:bg-code-bg-dark">
          <div className="mb-2">
            <span className="text-[10px] font-semibold uppercase text-text-secondary-light dark:text-text-secondary-dark">
              Args
            </span>
            <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-text-body-light dark:text-text-body-dark">
              {argsStr}
            </pre>
          </div>
          {tool.result !== undefined && (
            <div>
              <span className="text-[10px] font-semibold uppercase text-text-secondary-light dark:text-text-secondary-dark">
                Result
              </span>
              <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-text-primary-light dark:text-text-primary-dark">
                {tool.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
