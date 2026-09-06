// chips.tsx — small presentational atoms for the pools panel's effective
// rosters: provenance badges and read-only chips with an optional trailing
// action (veto / restore / remove). No emoji — text badges and lucide icons
// only, matching the settings tabs' conventions.

import type { ReactNode } from "react";

export type BadgeTone = "mute" | "brand" | "danger";

const BADGE_CLS: Record<BadgeTone, string> = {
  mute: "border-hairline text-mute",
  brand: "border-brand text-brand",
  danger: "border-danger text-danger",
};

export function Badge({ tone = "mute", children }: { tone?: BadgeTone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-pill border px-1.5 py-px text-xs ${BADGE_CLS[tone]}`}
    >
      {children}
    </span>
  );
}

interface ChipProps {
  children: ReactNode;
  /** Tooltip / accessible context (e.g. the provenance origin). */
  title?: string;
  /** Struck-through rendering for vetoed / inactive entries. */
  struck?: boolean;
  /** Trailing action button (icon + accessible label). */
  actionLabel?: string;
  actionIcon?: ReactNode;
  onAction?: () => void;
}

export function Chip({ children, title, struck, actionLabel, actionIcon, onAction }: ChipProps) {
  return (
    <span
      title={title}
      className={[
        "inline-flex min-h-7 items-center gap-1 rounded-pill border border-hairline bg-canvas px-2 font-mono text-xs",
        struck ? "text-faint line-through" : "text-body",
      ].join(" ")}
    >
      {children}
      {onAction && actionLabel ? (
        <button
          type="button"
          aria-label={actionLabel}
          title={actionLabel}
          onClick={onAction}
          className="-mr-1 inline-flex h-6 w-6 items-center justify-center rounded-full text-mute transition-colors duration-fast hover:bg-hairline-soft hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand"
        >
          {actionIcon}
        </button>
      ) : null}
    </span>
  );
}
