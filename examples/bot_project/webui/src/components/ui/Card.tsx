// Card.tsx — surface container primitive.
//
// Renders a rounded-md bordered surface over the elevated canvas. Pass
// `elevated` to add the floating shadow from the Geist palette. `id` is
// forwarded so callers can anchor scroll targets (e.g. provider cards).

import type { ReactNode } from "react";

export interface CardProps {
  children: ReactNode;
  className?: string;
  elevated?: boolean;
  /** Enables hover shadow-deepening + border strengthen. Use only on
   * interactive top-level cards (clickable/expandable); nested child cards
   * and pure-display cards should NOT enable this to avoid visual noise. */
  hoverable?: boolean;
  /** Forwarded to the rendered element so callers can anchor sticky bars etc. */
  id?: string;
}

export function Card({ children, className, elevated = false, hoverable = false, id }: CardProps) {
  const cls = [
    "rounded-md border border-hairline bg-canvas-elevated p-4",
    elevated ? "shadow-floating" : "",
    hoverable ? "card-hoverable" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return (
    <div className={cls} id={id}>
      {children}
    </div>
  );
}
