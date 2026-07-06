import { useState, type FC } from "react";
import { ChevronToggleIcon } from "./ui/icons";

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
        className="flex items-center gap-1 text-xs font-medium text-mute transition-colors hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-link/50"
      >
        <ChevronToggleIcon open={expanded} />
        Thinking
      </button>
      {expanded && (
        <div className="mt-1.5 rounded border-l-2 border-hairline bg-canvas-elevated p-3">
          <pre className="whitespace-pre-wrap break-words font-mono text-xs text-mute">
            {reasoning}
          </pre>
        </div>
      )}
    </div>
  );
};
