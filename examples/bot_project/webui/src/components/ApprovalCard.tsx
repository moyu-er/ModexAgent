import { useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ApprovalRequestView } from "../types/events";
import { Button } from "./ui/Button";
import { ChevronDownIcon } from "./ui/icons";
import { useT } from "../i18n";

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
// the higher tiers read as serious without glaring. Unknown tiers fall back to
// `normal`.
const TIER_BADGE: Record<string, string> = {
  normal:
    "border-severity-normal/40 bg-severity-normal/10 text-severity-normal",
  sensitive:
    "border-severity-sensitive/40 bg-severity-sensitive/10 text-severity-sensitive",
  dangerous:
    "border-severity-dangerous/40 bg-severity-dangerous/10 text-severity-dangerous",
  hardline:
    "border-severity-hardline/40 bg-severity-hardline/10 text-severity-hardline",
};

/** Inline pending-approval card. Shows tool name + tier and per-card
 *  [Approve] / [Deny All]. The tool arguments preview the first few lines by
 *  default; the chevron toggle at the bottom edge reveals the rest. Deny is
 *  batch-level: denying any card cancels the whole batch (backend preempts the
 *  rest). Cards only ever render pending requests — decided ones are dropped
 *  from the list by the hook. */
export function ApprovalCard({ view, onApprove, onDeny, disabled }: Props) {
  const t = useT();
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
    <div className="my-2 overflow-hidden rounded-md border border-hairline bg-canvas-elevated">
      <div className="p-3">
        <div className="flex items-center gap-2 text-sm">
          <span
            className={`rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${badgeClass}`}
          >
            {view.tier}
          </span>
          <span className="font-mono font-semibold text-ink">
            {view.tool_name}
          </span>
          <span className="text-xs text-mute">
            {t("approval.awaitingApproval")}
          </span>
        </div>

        <div className="mt-3 flex gap-2">
          <Button
            variant="primary"
            size="sm"
            disabled={disabled}
            onClick={() => onApprove(view.tool_call_id)}
          >
            {t("approval.approve")}
          </Button>
          <Button
            variant="danger"
            size="sm"
            disabled={disabled}
            onClick={() => onDeny(view.tool_call_id)}
          >
            {t("approval.denyAll")}
          </Button>
        </div>
      </div>

      {/* Tool arguments: clamped to the first few wrapped lines by default,
          full content when expanded. line-clamp counts visual lines, so a
          single very long value wraps and is truncated too. */}
      <div className="border-t border-hairline bg-canvas px-3 py-2">
        <pre
          ref={preRef}
          className={
            "whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-body" +
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
          aria-label={expanded ? t("approval.collapseArgs") : t("approval.expandArgs")}
            className="flex w-full items-center justify-center gap-1 border-t border-hairline py-1 text-[11px] text-mute transition-colors hover:bg-hairline-soft"
          >
            <span>{expanded ? t("approval.showLess") : t("approval.showMore")}</span>
            <ChevronDownIcon
              open={expanded}
              className="transition-transform duration-200"
            />
          </button>
      )}
    </div>
  );
}
