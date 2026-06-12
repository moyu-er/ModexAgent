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
        className="flex items-center gap-1 text-xs font-medium text-gray-400 hover:text-gray-300 transition-colors w-full text-left"
      >
        <span className="inline-block transition-transform duration-150 text-[10px]">
          {expanded ? "▼" : "▸"}
        </span>
        <span className="mr-1 text-[11px]">🔧</span>
        <span className="font-mono">{tool.tool}</span>
        {tool.result !== undefined && (
          <span className="ml-auto text-[10px] text-green-500">done</span>
        )}
      </button>
      {expanded && (
        <div className="mt-1 ml-5 p-2 rounded bg-gray-800 border border-gray-700">
          <div className="mb-1">
            <span className="text-[10px] font-semibold text-gray-500 uppercase">
              Args
            </span>
            <pre className="text-xs text-gray-400 font-mono whitespace-pre-wrap break-words mt-0.5">
              {argsStr}
            </pre>
          </div>
          {tool.result !== undefined && (
            <div>
              <span className="text-[10px] font-semibold text-gray-500 uppercase">
                Result
              </span>
              <pre className="text-xs text-gray-300 font-mono whitespace-pre-wrap break-words mt-0.5">
                {tool.result}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
};
