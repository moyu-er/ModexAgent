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
        className="flex w-full items-center gap-1 text-left text-xs font-medium text-gray-400 transition-colors hover:text-gray-200"
      >
        <span className="inline-block text-[10px] transition-transform duration-150">
          {expanded ? "▼" : "▸"}
        </span>
        <span className="mr-1 text-[11px]">🔧</span>
        <span className="font-mono">{tool.tool}</span>
        {tool.result !== undefined && (
          <span className="ml-auto text-[10px] text-emerald-400">done</span>
        )}
      </button>
      {expanded && (
        <div className="mt-1.5 ml-5 rounded border border-white/10 bg-ink-800 p-3">
          <div className="mb-2">
            <span className="text-[10px] font-semibold uppercase text-gray-500">
              Args
            </span>
            <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-gray-400">
              {argsStr}
            </pre>
          </div>
          {tool.result !== undefined && (
            <div>
              <span className="text-[10px] font-semibold uppercase text-gray-500">
                Result
              </span>
              <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs text-gray-300">
                {tool.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
