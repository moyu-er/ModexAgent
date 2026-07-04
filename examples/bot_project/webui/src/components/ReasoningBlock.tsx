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
        className="flex items-center gap-1 text-xs font-medium text-text-secondary transition-colors hover:text-text-primary"
      >
        <span className="inline-block text-[10px] transition-transform duration-150">
          {expanded ? "▼" : "▸"}
        </span>
        Thinking
      </button>
      {expanded && (
        <div className="mt-1.5 rounded border-l-2 border-quote-border bg-quote-bg p-3">
          <pre className="whitespace-pre-wrap break-words font-mono text-xs text-text-secondary">
            {reasoning}
          </pre>
        </div>
      )}
    </div>
  );
};
