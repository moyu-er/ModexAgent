import { useState, type FC } from "react";

export interface ReasoningBlockProps {
  reasoning: string;
}

export const ReasoningBlock: FC<ReasoningBlockProps> = ({ reasoning }) => {
  const [expanded, setExpanded] = useState(false);

  if (!reasoning) {
    return null;
  }

  const toggle = (): void => {
    setExpanded((prev) => !prev);
  };

  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={toggle}
        className="flex items-center gap-1 text-xs font-medium text-gray-400 hover:text-gray-300 transition-colors"
      >
        <span className="inline-block transition-transform duration-150 text-[10px]">
          {expanded ? "▼" : "▸"}
        </span>
        Thinking
      </button>
      {expanded && (
        <div className="mt-1 p-3 rounded bg-gray-800 border-l-2 border-gray-600">
          <pre className="text-xs text-gray-400 font-mono whitespace-pre-wrap break-words">
            {reasoning}
          </pre>
        </div>
      )}
    </div>
  );
};
