import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ApprovalRequestView } from "../types/events";

interface Props {
  view: ApprovalRequestView;
  onApprove: (toolCallId: string) => void;
  onDeny: (toolCallId: string) => void;
  /** Batch-level submit lock: disables both actions while any POST is in flight. */
  disabled?: boolean;
}

// How many visual (post-wrap) lines of the serialized arguments to show before
// the user expands. A single very long value wraps and counts toward this, so
// the preview stays compact regardless of how the JSON is shaped.
const PREVIEW_MAX_LINES = 3;

// Severity-tier badge styling. Each tier gets a calm, low-saturation accent so
// the higher tiers read as serious without glaring. Light/dark variants follow
// the severity token convention (-light = light-mode text, -dark = dark-mode
// text). Unknown tiers fall back to `normal`.
const TIER_BADGE: Record<string, string> = {
  normal:
    "border-severity-normal-light/40 bg-severity-normal-light/10 text-severity-normal-light dark:border-severity-normal-dark/40 dark:bg-severity-normal-dark/10 dark:text-severity-normal-dark",
  sensitive:
    "border-severity-sensitive-light/40 bg-severity-sensitive-light/10 text-severity-sensitive-light dark:border-severity-sensitive-dark/40 dark:bg-severity-sensitive-dark/10 dark:text-severity-sensitive-dark",
  dangerous:
    "border-severity-dangerous-light/40 bg-severity-dangerous-light/10 text-severity-dangerous-light dark:border-severity-dangerous-dark/40 dark:bg-severity-dangerous-dark/10 dark:text-severity-dangerous-dark",
  hardline:
    "border-severity-hardline-light/40 bg-severity-hardline-light/10 text-severity-hardline-light dark:border-severity-hardline-dark/40 dark:bg-severity-hardline-dark/10 dark:text-severity-hardline-dark",
};

/** Inline pending-approval card. Shows tool name + tier and per-card
 *  [Approve] / [Deny All]. The tool arguments preview the first few lines by
 *  default; the chevron toggle at the bottom edge reveals the rest. Deny is
 *  batch-level: denying any card cancels the whole batch (backend preempts the
 *  rest). Cards only ever render pending requests — decided ones are dropped
 *  from the list by the hook. */
export function ApprovalCard({ view, onApprove, onDeny, disabled }: Props) {
  const [expanded, setExpanded] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);
  // Whether the clamped preview hides content. Drives the chevron's presence.
  const [overflowing, setOverflowing] = useState(false);

  // Memoize the serialization so it runs once per `view.arguments`, not on
  // every parent re-render (the card re-renders on each isApprovingBatch
  // toggle and any hook state change while cards are on screen).
  const fullArgs = useMemo(
    () => JSON.stringify(view.arguments, null, 2),
    [view.arguments],
  );

  // Cheap content signal that works without layout (e.g. in jsdom): more
  // logical lines than the preview definitely overflows.
  const multiLineOverflow = useMemo(
    () => fullArgs.split("\n").length > PREVIEW_MAX_LINES,
    [fullArgs],
  );

  // Measure the clamped <pre> for the single-long-line case: one logical line
  // that wraps past the preview height. Only trustworthy while collapsed (the
  // clamp is off when expanded).
  useLayoutEffect(() => {
    if (expanded) return;
    const el = preRef.current;
    if (!el) return;
    setOverflowing(el.scrollHeight > el.clientHeight + 1);
  }, [fullArgs, expanded]);

  const hasMore = multiLineOverflow || overflowing;
  const toggle = (): void => setExpanded((prev) => !prev);

  const badgeClass = TIER_BADGE[view.tier] ?? TIER_BADGE.normal;

  return (
    <div className="my-2 overflow-hidden rounded-lg border border-card-border-light bg-content-bg-light dark:border-card-border-dark dark:bg-content-bg-dark">
      <div className="p-3">
        <div className="flex items-center gap-2 text-sm">
          <span
            className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${badgeClass}`}
          >
            {view.tier}
          </span>
          <span className="font-mono font-semibold text-text-primary-light dark:text-text-primary-dark">
            {view.tool_name}
          </span>
          <span className="text-xs text-text-secondary-light dark:text-text-secondary-dark">
            awaiting approval
          </span>
        </div>

        <div className="mt-3 flex gap-2">
          <button
            type="button"
            disabled={disabled}
            onClick={() => onApprove(view.tool_call_id)}
            className="rounded border border-approve-light/50 px-3 py-1 text-sm font-medium text-approve-light transition-colors hover:bg-approve-light/10 disabled:cursor-not-allowed disabled:opacity-50 dark:border-approve-dark/50 dark:text-approve-dark dark:hover:bg-approve-dark/10"
          >
            Approve
          </button>
          <button
            type="button"
            disabled={disabled}
            onClick={() => onDeny(view.tool_call_id)}
            className="rounded border border-deny-light/50 px-3 py-1 text-sm font-medium text-deny-light transition-colors hover:bg-deny-light/10 disabled:cursor-not-allowed disabled:opacity-50 dark:border-deny-dark/50 dark:text-deny-dark dark:hover:bg-deny-dark/10"
          >
            Deny All
          </button>
        </div>
      </div>

      {/* Tool arguments: clamped to the first few wrapped lines by default,
          full content when expanded. line-clamp counts visual lines, so a
          single very long value wraps and is truncated too. */}
      <div className="border-t border-divider-light bg-code-bg-light px-3 py-2 dark:border-divider-dark dark:bg-code-bg-dark">
        <pre
          ref={preRef}
          className={
            "whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-text-body-light dark:text-text-body-dark" +
            (expanded ? "" : " line-clamp-3")
          }
        >
          {fullArgs}
        </pre>
      </div>

      {/* Bottom chevron toggle: only shown when the preview hides content. */}
      {hasMore && (
        <button
          type="button"
          onClick={toggle}
          aria-expanded={expanded}
          aria-label={expanded ? "Collapse arguments" : "Expand arguments"}
          className="flex w-full items-center justify-center gap-1 border-t border-divider-light py-1 text-[11px] text-text-secondary-light transition-colors hover:bg-sidebar-hover-light dark:border-divider-dark dark:text-text-secondary-dark dark:hover:bg-sidebar-hover-dark"
        >
          <span>{expanded ? "Show less" : "Show more"}</span>
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            className={"transition-transform duration-200" + (expanded ? " rotate-180" : "")}
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </button>
      )}
    </div>
  );
}
