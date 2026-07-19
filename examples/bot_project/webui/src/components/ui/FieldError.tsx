// FieldError.tsx — error copy under a form control (12px danger + icon, §5.2).
// Renders with `role="alert"` so screen readers announce it as soon as it
// mounts. Use it instead of HelperText when a field fails validation.

import type { ReactNode } from "react";

export interface FieldErrorProps {
  children: ReactNode;
  className?: string;
  id?: string;
}

export function FieldError({ children, className, id }: FieldErrorProps) {
  const cls = ["mt-1 flex items-start gap-1 text-xs text-danger", className]
    .filter(Boolean)
    .join(" ");
  return (
    <p id={id} className={cls} role="alert">
      <svg
        className="mt-px h-3 w-3 shrink-0"
        viewBox="0 0 16 16"
        fill="none"
        aria-hidden="true"
      >
        <circle cx="8" cy="8" r="6.25" stroke="currentColor" strokeWidth="1.5" />
        <path d="M8 5v3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        <circle cx="8" cy="11" r="0.9" fill="currentColor" />
      </svg>
      <span>{children}</span>
    </p>
  );
}
