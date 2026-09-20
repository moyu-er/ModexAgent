// chips.tsx — small presentational atoms for the pools panel (provenance
// badges). No emoji — text badges only, matching the settings tabs'
// conventions.

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
