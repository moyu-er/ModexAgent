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
  /** Forwarded to the rendered element so callers can anchor sticky bars etc. */
  id?: string;
}

export function Card({ children, className, elevated = false, id }: CardProps) {
  const cls = [
    "rounded-md border border-hairline bg-canvas-elevated p-4",
    elevated ? "shadow-floating" : "",
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
