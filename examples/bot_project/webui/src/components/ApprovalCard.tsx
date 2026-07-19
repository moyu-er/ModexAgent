import { useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { AlertTriangle, OctagonAlert, ShieldAlert, ShieldCheck, type LucideIcon } from "lucide-react";
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

// Severity presentation (§6): each tier is a 3px left bar color (--sev on the
// shared trace-card shell) + a status icon + the tier text label — severity is
// never conveyed by color alone. Unknown tiers fall back to `normal`.
const TIER_PRESENTATION: Record<
  string,
  { sev: string; cls: string; Icon: LucideIcon }
> = {
  normal: {
    sev: "var(--color-severity-normal)",
    cls: "text-severity-normal",
    Icon: ShieldCheck,
  },
  sensitive: {
    sev: "var(--color-severity-sensitive)",
    cls: "text-severity-sensitive",
    Icon: AlertTriangle,
  },
  dangerous: {
    sev: "var(--color-severity-dangerous)",
    cls: "text-severity-dangerous",
    Icon: ShieldAlert,
  },
  hardline: {
    sev: "var(--color-severity-hardline)",
    cls: "text-severity-hardline",
    Icon: OctagonAlert,
  },
};

/** Inline pending-approval card (§6): shares the trace-card language —
 *  elevated surface, mono eyebrow header, 3px severity left bar + status
 *  icon. Actions are primary (approve) + ghost (reject). The tool arguments
 *  preview the first few lines by default; the chevron toggle at the bottom
 *  edge reveals the rest. Deny is batch-level: denying any card cancels the
 *  whole batch (backend preempts the rest). Cards only ever render pending
 *  requests — decided ones are dropped from the list by the hook. */
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

  const tier = TIER_PRESENTATION[view.tier] ?? TIER_PRESENTATION.normal!;

  return (
    <div
      className="trace-card my-2"
      style={{ "--sev": tier.sev } as CSSProperties}
    >
      <div className="p-3">
        <div className="flex items-center gap-2 text-base">
          <span className="eyebrow">{t("approval.eyebrow")}</span>
          <span data-severity-icon className={`flex items-center ${tier.cls}`}>
            <tier.Icon size={14} aria-hidden={true} />
          </span>
          <span className="font-mono font-semibold text-ink">
            {view.tool_name}
          </span>
          <span
            className={`text-xs font-semibold uppercase tracking-eyebrow ${tier.cls}`}
          >
            {view.tier}
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
            variant="ghost"
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
            className="flex w-full items-center justify-center gap-1 border-t border-hairline py-1 text-xs text-mute transition-colors hover:bg-hairline-soft"
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
