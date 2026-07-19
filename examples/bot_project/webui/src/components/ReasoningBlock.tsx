import { useState, type FC } from "react";
import { ChevronToggleIcon } from "./ui/icons";
import { useT } from "../i18n";

export interface ReasoningBlockProps {
  reasoning: string;
}

/** Collapsible reasoning block (§6): sans chat-label header + chevron; the
 *  body is dim sans text behind a 2px brand-alpha left border
 *  (`.reasoning-body`). Mono was retired so thinking prose reads naturally. */
export const ReasoningBlock: FC<ReasoningBlockProps> = ({ reasoning }) => {
  const t = useT();
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
        aria-expanded={expanded}
        className="flex items-center gap-1.5 transition-colors hover:brightness-125 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
      >
        <ChevronToggleIcon open={expanded} className="text-mute" />
        <span className="chat-label text-mute">{t("reasoning.label")}</span>
      </button>
      {expanded && (
        <div className="reasoning-body mt-1.5">
          <pre className="whitespace-pre-wrap break-words text-xs leading-snug text-mute">
            {reasoning}
          </pre>
        </div>
      )}
    </div>
  );
};
