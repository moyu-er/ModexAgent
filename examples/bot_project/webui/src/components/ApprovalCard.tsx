import { useMemo, useState } from "react";
import type { ApprovalRequestView } from "../types/events";

interface Props {
  view: ApprovalRequestView;
  onApprove: (toolCallId: string) => void;
  onDeny: (toolCallId: string) => void;
  /** Batch-level submit lock: disables both actions while any POST is in flight. */
  disabled?: boolean;
}

// Truncation budget for the collapsed preview. Keeps the card compact before
// the user expands it; the full JSON is always one toggle away.
const PREVIEW_MAX_CHARS = 120;

/** Inline pending-approval card. Shows tool name + tier, a truncated args
 *  preview that expands to full JSON, and per-card [Approve] / [Deny All].
 *  Deny is batch-level: denying any card cancels the whole batch (backend
 *  preempts the rest). Cards only ever render pending requests — decided
 *  ones are dropped from the list by the hook. */
export function ApprovalCard({ view, onApprove, onDeny, disabled }: Props) {
  const [expanded, setExpanded] = useState(false);

  // Memoize the serialization so it runs once per `view.arguments`, not on
  // every parent re-render (the card re-renders on each isApprovingBatch
  // toggle and any hook state change while cards are on screen).
  const fullArgs = useMemo(
    () => JSON.stringify(view.arguments, null, 2),
    [view.arguments],
  );
  const previewArgs = useMemo(() => {
    const isLong = fullArgs.length > PREVIEW_MAX_CHARS;
    return isLong ? `${fullArgs.slice(0, PREVIEW_MAX_CHARS)}…` : fullArgs;
  }, [fullArgs]);

  const toggle = (): void => setExpanded((prev) => !prev);

  return (
    <div className="my-2 rounded-lg border border-card-border-light bg-content-bg-light p-3 dark:border-card-border-dark dark:bg-content-bg-dark">
      <div className="flex items-center gap-2 text-sm">
        <span className="rounded border border-warning-light bg-warning-light/10 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-warning-dark dark:border-warning-dark dark:bg-warning-dark/10 dark:text-warning-light">
          {view.tier}
        </span>
        <span className="font-mono font-semibold text-text-primary-light dark:text-text-primary-dark">
          {view.tool_name}
        </span>
        <span className="text-xs text-text-secondary-light dark:text-text-secondary-dark">
          awaiting approval
        </span>
      </div>

      <button
        type="button"
        onClick={toggle}
        aria-expanded={expanded}
        aria-label={expanded ? "Collapse arguments" : "Expand arguments"}
        className="mt-2 flex w-full items-start gap-1 rounded border border-code-border-light bg-code-bg-light px-2 py-1.5 text-left font-mono text-xs text-text-body-light transition-colors hover:border-divider-light dark:border-code-border-dark dark:bg-code-bg-dark dark:text-text-body-dark dark:hover:border-divider-dark"
      >
        <span className="inline-block shrink-0 text-[10px] leading-relaxed text-text-secondary-light dark:text-text-secondary-dark">
          {expanded ? "▼" : "▸"}
        </span>
        <pre className="whitespace-pre-wrap break-words leading-relaxed">
          {expanded ? fullArgs : previewArgs}
        </pre>
      </button>

      <div className="mt-2 flex gap-2">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onApprove(view.tool_call_id)}
          className="rounded border border-success-light px-3 py-1 text-sm font-medium text-success-dark transition-colors hover:bg-success-light/10 disabled:cursor-not-allowed disabled:opacity-50 dark:border-success-dark dark:text-success-light dark:hover:bg-success-dark/10"
        >
          Approve
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => onDeny(view.tool_call_id)}
          className="rounded border border-error-light px-3 py-1 text-sm font-medium text-error-dark transition-colors hover:bg-error-light/10 disabled:cursor-not-allowed disabled:opacity-50 dark:border-error-dark dark:text-error-light dark:hover:bg-error-dark/10"
        >
          Deny All
        </button>
      </div>
    </div>
  );
}
