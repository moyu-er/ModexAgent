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
        className="flex items-center gap-1 text-xs font-medium text-text-secondary-light transition-colors hover:text-text-primary-light dark:text-text-secondary-dark dark:hover:text-text-primary-dark"
      >
        <span className="inline-block text-[10px] transition-transform duration-150">
          {expanded ? "▼" : "▸"}
        </span>
        Thinking
      </button>
      {expanded && (
        <div className="mt-1.5 rounded border-l-2 border-quote-border-light bg-quote-bg-light p-3 dark:border-quote-border-dark dark:bg-quote-bg-dark">
          <pre className="whitespace-pre-wrap break-words font-mono text-xs text-text-secondary-light dark:text-text-secondary-dark">
            {reasoning}
          </pre>
        </div>
      )}
    </div>
  );
};
